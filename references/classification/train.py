import datetime
import os
import time
import warnings
import copy

import presets
import torch
import torch.utils.data
import torchvision
import transforms
import utils
from sampler import RASampler
from torch import nn
from torch.utils.data.dataloader import default_collate
from torchvision.transforms.functional import InterpolationMode

import dataset_utils
import model_utils
import edgeai_torchmodelopt


def train_one_epoch(model, criterion, optimizer, data_loader, device, epoch, args, model_ema=None, scaler=None):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter("lr", utils.SmoothedValue(window_size=1, fmt="{value}"))
    metric_logger.add_meter("img/s", utils.SmoothedValue(window_size=10, fmt="{value}"))
    dataset_len = len(data_loader)

    header = f"Epoch: [{epoch}]"
    for i, (image, target) in enumerate(metric_logger.log_every(data_loader, args.print_freq, header)):
        start_time = time.time()
        image, target = image.to(device), target.to(device)
        with torch.cuda.amp.autocast(enabled=scaler is not None):
            output = model(image)
            loss = criterion(output, target)

        optimizer.zero_grad()
        if scaler is not None:
            scaler.scale(loss).backward()
            if args.clip_grad_norm is not None:
                # we should unscale the gradients of optimizer's assigned params if do gradient clipping
                scaler.unscale_(optimizer)
                nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            if args.clip_grad_norm is not None:
                nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad_norm)
            optimizer.step()

        if model_ema and i % args.model_ema_steps == 0:
            model_ema.update_parameters(model)
            if epoch < args.lr_warmup_epochs:
                # Reset ema buffer to keep copying weights during warmup period
                model_ema.n_averaged.fill_(0)

        acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
        batch_size = image.shape[0]
        metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]["lr"])
        metric_logger.meters["acc1"].update(acc1.item(), n=batch_size)
        metric_logger.meters["acc5"].update(acc5.item(), n=batch_size)
        metric_logger.meters["img/s"].update(batch_size / (time.time() - start_time))
        if args.train_epoch_size_factor and i >= round(args.train_epoch_size_factor * dataset_len):
            break


def evaluate(args, model, criterion, data_loader, device, print_freq=100, log_suffix=""):
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = f"Test: {log_suffix}"
    dataset_len = len(data_loader)

    num_processed_samples = 0
    with torch.inference_mode():
        for i, (image, target) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
            image = image.to(device, non_blocking=True)
            target = target.to(device, non_blocking=True)
            output = model(image)
            loss = criterion(output, target)

            acc1, acc5 = utils.accuracy(output, target, topk=(1, 5))
            # FIXME need to take into account that the datasets
            # could have been padded in distributed setup
            batch_size = image.shape[0]
            metric_logger.update(loss=loss.item())
            metric_logger.meters["acc1"].update(acc1.item(), n=batch_size)
            metric_logger.meters["acc5"].update(acc5.item(), n=batch_size)
            num_processed_samples += batch_size
            if args.val_epoch_size_factor and i >= round(args.val_epoch_size_factor * dataset_len):
                break
    # gather the stats from all processes

    num_processed_samples = utils.reduce_across_processes(num_processed_samples)
    if (
        hasattr(data_loader.dataset, "__len__")
        and len(data_loader.dataset) != num_processed_samples
        and (not args.distributed or torch.distributed.get_rank() == 0)
    ):
        # See FIXME above
        warnings.warn(
            f"It looks like the dataset has {len(data_loader.dataset)} samples, but {num_processed_samples} "
            "samples were used for the validation, which might bias the results. "
            "Try adjusting the batch size and / or the world size. "
            "Setting the world size to 1 is always a safe bet."
        )

    metric_logger.synchronize_between_processes()

    print(f"{header} Acc@1 {metric_logger.acc1.global_avg:.3f} Acc@5 {metric_logger.acc5.global_avg:.3f}")
    return metric_logger.acc1.global_avg


def _get_cache_path(filepath):
    import hashlib

    h = hashlib.sha1(filepath.encode()).hexdigest()
    cache_path = os.path.join("~", ".torch", "vision", "datasets", "imagefolder", h[:10] + ".pt")
    cache_path = os.path.expanduser(cache_path)
    return cache_path


def load_data(traindir, valdir, args):
    # Data loading code
    print("Loading data")
    val_resize_size, val_crop_size, train_crop_size = (
        args.val_resize_size,
        args.val_crop_size,
        args.train_crop_size,
    )
    interpolation = InterpolationMode(args.interpolation)

    print("Loading training data")
    st = time.time()
    cache_path = _get_cache_path(traindir)
    if args.cache_dataset and os.path.exists(cache_path):
        # Attention, as the transforms are also cached!
        print(f"Loading dataset_train from {cache_path}")
        dataset, _ = torch.load(cache_path)
    else:
        # We need a default value for the variables below because args may come
        # from train_quantization.py which doesn't define them.
        auto_augment_policy = getattr(args, "auto_augment", None)
        random_erase_prob = getattr(args, "random_erase", 0.0)
        ra_magnitude = getattr(args, "ra_magnitude", None)
        augmix_severity = getattr(args, "augmix_severity", None)
        train_transform = presets.ClassificationPresetTrain(
	                crop_size=train_crop_size,
	                interpolation=interpolation,
	                auto_augment_policy=auto_augment_policy,
	                random_erase_prob=random_erase_prob,
	                ra_magnitude=ra_magnitude,
	                augmix_severity=augmix_severity,
	    )
        if args.dataset == 'modelmaker':
            train_folders = os.path.normpath(traindir).split(os.sep)
            train_anno = os.path.join(os.sep.join(train_folders[:-1]), 'annotations', f'{args.annotation_prefix}_train.json')
            dataset = dataset_utils.CocoClassification(traindir, train_anno, train_transform)
        else:
            dataset = torchvision.datasets.ImageFolder(
	            traindir,
	            train_transform,
            )
		#
        if args.cache_dataset:
            print(f"Saving dataset_train to {cache_path}")
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset, traindir), cache_path)
    print("Took", time.time() - st)

    print("Loading validation data")
    cache_path = _get_cache_path(valdir)
    if args.cache_dataset and os.path.exists(cache_path):
        # Attention, as the transforms are also cached!
        print(f"Loading dataset_test from {cache_path}")
        dataset_test, _ = torch.load(cache_path)
    else:
        if args.weights_enum and args.test_only:
            weights = torchvision.models.get_weight(args.weights_enum)
            preprocessing = weights.transforms()
        else:
            preprocessing = presets.ClassificationPresetEval(
                crop_size=val_crop_size, resize_size=val_resize_size, interpolation=interpolation
            )

        if args.dataset == 'modelmaker':
            val_folders = os.path.normpath(valdir).split(os.sep)
            val_anno = os.path.join(os.sep.join(val_folders[:-1]), 'annotations', f'{args.annotation_prefix}_val.json')
            dataset_test = dataset_utils.CocoClassification(valdir, val_anno, preprocessing)
        else:
            dataset_test = torchvision.datasets.ImageFolder(
                valdir,
                preprocessing,
            )
        #
        if args.cache_dataset:
            print(f"Saving dataset_test to {cache_path}")
            utils.mkdir(os.path.dirname(cache_path))
            utils.save_on_master((dataset_test, valdir), cache_path)

    print("Creating data loaders")
    if args.distributed:
        if hasattr(args, "ra_sampler") and args.ra_sampler:
            train_sampler = RASampler(dataset, shuffle=True, repetitions=args.ra_reps)
        else:
            train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test, shuffle=False)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    return dataset, dataset_test, train_sampler, test_sampler


def export_model(args, model, epoch, model_name):
    export_device = next(model.parameters()).device
    if args.quantization:
        if hasattr(model, "convert"):
            model = model.convert()
        else:
            model = torch.ao.quantization.quantize_fx.convert_fx(model)
        #
    #
    example_input = torch.rand((1,3,args.val_crop_size,args.val_crop_size), device=export_device)
    utils.export_on_master(model, example_input, os.path.join(args.output_dir, model_name), opset_version=args.opset_version)


def split_weights(weights_name):
    weights_list = weights_name.split(',')
    weights_urls = []
    weights_enums = []
    for w in weights_list:
        w = w.lstrip()
        if edgeai_torchmodelopt.xnn.utils.is_url_or_file(w):
            weights_urls.append(w)
        else:
            weights_enums.append(w)
        #
    #
    return ((weights_urls[0] if len(weights_urls)>0 else None), (weights_enums[0] if len(weights_enums)>0 else None))


def main(args):
    if args.output_dir:
        utils.mkdir(args.output_dir)

    # create logger that tee writes to file
    logger = edgeai_torchmodelopt.xnn.utils.TeeLogger(os.path.join(args.output_dir, 'run.log'))

    # weights can be an external url or a pretrained enum in torhvision
    (args.weights_url, args.weights_enum) = split_weights(args.weights)
    
    utils.init_distributed_mode(args)
    print(args)

    if args.distributed and args.parallel:
        raise RuntimeError("both DistributedDataParallel and DataParallel cannot be used simultaneously")

    device = torch.device(args.device)

    if args.use_deterministic_algorithms:
        torch.backends.cudnn.benchmark = False
        torch.use_deterministic_algorithms(True)
    else:
        torch.backends.cudnn.benchmark = True

    train_dir = os.path.join(args.data_path, "train")
    val_dir = os.path.join(args.data_path, "val")
    dataset, dataset_test, train_sampler, test_sampler = load_data(train_dir, val_dir, args)

    collate_fn = None
    num_classes = len(dataset.classes)
    mixup_transforms = []
    if args.mixup_alpha > 0.0:
        mixup_transforms.append(transforms.RandomMixup(num_classes, p=1.0, alpha=args.mixup_alpha))
    if args.cutmix_alpha > 0.0:
        mixup_transforms.append(transforms.RandomCutmix(num_classes, p=1.0, alpha=args.cutmix_alpha))
    if mixup_transforms:
        mixupcutmix = torchvision.transforms.RandomChoice(mixup_transforms)

        def collate_fn(batch):
            return mixupcutmix(*default_collate(batch))

    data_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        sampler=train_sampler,
        num_workers=args.workers,
        pin_memory=True,
        collate_fn=collate_fn,
    )
    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=args.batch_size, sampler=test_sampler, num_workers=args.workers, pin_memory=True
    )

    print("Creating model")
    model, surgery_kwargs = model_utils.get_model(args.model, weights=args.weights_enum, num_classes=num_classes, model_surgery=args.model_surgery)

    if args.model_surgery == edgeai_torchmodelopt.xmodelopt.surgery.SyrgeryVersion.SURGERY_LEGACY:
        model = edgeai_torchmodelopt.xmodelopt.surgery.v1.convert_to_lite_model(model, **surgery_kwargs)
    elif args.model_surgery == edgeai_torchmodelopt.xmodelopt.surgery.SyrgeryVersion.SURGERY_FX:
        model = edgeai_torchmodelopt.xmodelopt.surgery.v2.convert_to_lite_fx(model)

    if args.weights_url and (not args.test_only):
        print(f"loading pretrained checkpoint for training: {args.weights_url}")
        edgeai_torchmodelopt.xnn.utils.load_weights(model, args.weights_url, state_dict_name=args.weights_state_dict_name)

    if args.pruning == edgeai_torchmodelopt.xmodelopt.pruning.PruningVersion.PRUNING_LEGACY:
        assert False, "Pruning is currently not supported in the legacy modules based method"
    elif args.pruning == edgeai_torchmodelopt.xmodelopt.pruning.PruningVersion.PRUNING_FX: #2
        model = edgeai_torchmodelopt.xmodelopt.pruning.PrunerModule(model, pruning_ratio=args.pruning_ratio, total_epochs=args.epochs, pruning_init_train_ep=args.pruning_init_train_ep,
                                            pruning_class=args.pruning_class, pruning_type=args.pruning_type, pruning_global=args.pruning_global, pruning_m=args.pruning_m)
    
    if args.quantization == edgeai_torchmodelopt.xmodelopt.quantization.QuantizationVersion.QUANTIZATION_LEGACY:
        dummy_input = torch.rand(1,3,args.val_crop_size,args.val_crop_size)
        model = edgeai_torchmodelopt.xmodelopt.quantization.v1.QuantTrainModule(model, dummy_input=dummy_input, total_epochs=args.epochs)
    elif args.quantization == edgeai_torchmodelopt.xmodelopt.quantization.QuantizationVersion.QUANTIZATION_FX:
        model = edgeai_torchmodelopt.xmodelopt.quantization.v2.QATFxModule(model, total_epochs=args.epochs, qconfig_type=args.quantization_type)

    if args.weights_url and args.test_only:
        print(f"loading pretrained checkpoint for test: {args.weights_url}")
        edgeai_torchmodelopt.xnn.utils.load_weights(model, args.weights_url, state_dict_name=args.weights_state_dict_name)

    model.to(device)

    if args.distributed and args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if args.compile_model:
        print("Compiling the model using PyTorch2.0 functionality")
        model = torch.compile(model)

    criterion = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)

    custom_keys_weight_decay = []
    if args.bias_weight_decay is not None:
        custom_keys_weight_decay.append(("bias", args.bias_weight_decay))
    if args.transformer_embedding_decay is not None:
        for key in ["class_token", "position_embedding", "relative_position_bias_table"]:
            custom_keys_weight_decay.append((key, args.transformer_embedding_decay))
    parameters = utils.set_weight_decay(
        model,
        args.weight_decay,
        norm_weight_decay=args.norm_weight_decay,
        custom_keys_weight_decay=custom_keys_weight_decay if len(custom_keys_weight_decay) > 0 else None,
    )

    opt_name = args.opt.lower()
    if opt_name.startswith("sgd"):
        # in general use torch.optim.SGD
        # AdaptiveSGD is required only if you want to freeze certain parameters by setting requires_update = False. 
        optimizer = edgeai_torchmodelopt.xnn.optim.AdaptiveSGD(
            parameters,
            lr=args.lr,
            momentum=args.momentum,
            weight_decay=args.weight_decay,
            nesterov="nesterov" in opt_name,
        )
    elif opt_name == "rmsprop":
        optimizer = torch.optim.RMSprop(
            parameters, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay, eps=0.0316, alpha=0.9
        )
    elif opt_name == "adamw":
        optimizer = torch.optim.AdamW(parameters, lr=args.lr, weight_decay=args.weight_decay)
    else:
        raise RuntimeError(f"Invalid optimizer {args.opt}. Only SGD, RMSprop and AdamW are supported.")

    scaler = torch.cuda.amp.GradScaler() if args.amp else None

    args.lr_scheduler = args.lr_scheduler.lower()
    if args.lr_scheduler == "steplr":
        main_lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_step_size, gamma=args.lr_gamma)
    elif args.lr_scheduler == "cosineannealinglr":
        main_lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=args.epochs - args.lr_warmup_epochs, eta_min=args.lr_min
        )
    elif args.lr_scheduler == "exponentiallr":
        main_lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=args.lr_gamma)
    else:
        raise RuntimeError(
            f"Invalid lr scheduler '{args.lr_scheduler}'. Only StepLR, CosineAnnealingLR and ExponentialLR "
            "are supported."
        )

    if args.lr_warmup_epochs > 0:
        if args.lr_warmup_method == "linear":
            warmup_lr_scheduler = torch.optim.lr_scheduler.LinearLR(
                optimizer, start_factor=args.lr_warmup_decay, total_iters=args.lr_warmup_epochs
            )
        elif args.lr_warmup_method == "constant":
            warmup_lr_scheduler = torch.optim.lr_scheduler.ConstantLR(
                optimizer, factor=args.lr_warmup_decay, total_iters=args.lr_warmup_epochs
            )
        else:
            raise RuntimeError(
                f"Invalid warmup lr method '{args.lr_warmup_method}'. Only linear and constant are supported."
            )
        lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
            optimizer, schedulers=[warmup_lr_scheduler, main_lr_scheduler], milestones=[args.lr_warmup_epochs]
        )
    else:
        lr_scheduler = main_lr_scheduler

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        model_without_ddp = model.module
    elif args.parallel:
        model = torch.nn.parallel.DataParallel(model)
        model_without_ddp = model.module

    model_ema = None
    if args.model_ema:
        # Decay adjustment that aims to keep the decay independent of other hyper-parameters originally proposed at:
        # https://github.com/facebookresearch/pycls/blob/f8cd9627/pycls/core/net.py#L123
        #
        # total_ema_updates = (Dataset_size / n_GPUs) * epochs / (batch_size_per_gpu * EMA_steps)
        # We consider constant = Dataset_size for a given dataset/setup and omit it. Thus:
        # adjust = 1 / total_ema_updates ~= n_GPUs * batch_size_per_gpu * EMA_steps / epochs
        adjust = args.world_size * args.batch_size * args.model_ema_steps / args.epochs
        alpha = 1.0 - args.model_ema_decay
        alpha = min(1.0, alpha * adjust)
        model_ema = utils.ExponentialMovingAverage(model_without_ddp, device=device, decay=1.0 - alpha)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location="cpu")
        model_without_ddp.load_state_dict(checkpoint["model"])
        if not args.test_only:
            optimizer.load_state_dict(checkpoint["optimizer"])
            lr_scheduler.load_state_dict(checkpoint["lr_scheduler"])
        args.start_epoch = checkpoint["epoch"] + 1
        if model_ema:
            model_ema.load_state_dict(checkpoint["model_ema"])
        if scaler:
            scaler.load_state_dict(checkpoint["scaler"])

    if args.test_only:
        # We disable the cudnn benchmarking because it can noticeably affect the accuracy
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        if model_ema:
            evaluate(args, model_ema, criterion, data_loader_test, device=device, log_suffix="EMA")
        else:
            evaluate(args, model, criterion, data_loader_test, device=device)
        export_model(args, model_without_ddp, 0, "model_test.onnx")
        return

    print("Start training")
    start_time = time.time()
    best_acc = 0
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        train_one_epoch(model, criterion, optimizer, data_loader, device, epoch, args, model_ema, scaler)
        lr_scheduler.step()
        epoch_acc = evaluate(args, model, criterion, data_loader_test, device=device)
        if model_ema:
            epoch_acc = evaluate(args, model_ema, criterion, data_loader_test, device=device, log_suffix="EMA")
        if args.output_dir:
            checkpoint = {
                "model": model_without_ddp.state_dict(),
                "optimizer": optimizer.state_dict(),
                "lr_scheduler": lr_scheduler.state_dict(),
                "epoch": epoch,
                "args": args,
            }
            if model_ema:
                checkpoint["model_ema"] = model_ema.state_dict()
            if scaler:
                checkpoint["scaler"] = scaler.state_dict()
            if epoch == 0 or epoch >= (args.epochs-10):
                utils.save_on_master(checkpoint, os.path.join(args.output_dir, f"checkpoint_{epoch}.pth"))
                export_model(args, model_without_ddp, epoch, f"model_{epoch}.onnx")
            if epoch_acc >= best_acc:
                utils.save_on_master(checkpoint, os.path.join(args.output_dir, "checkpoint.pth"))
                export_model(args, model_without_ddp, epoch, f"model.onnx")
                best_acc = epoch_acc

    logger.close()
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print(f"Training time {total_time_str}")


def get_args_parser(add_help=True):
    import argparse

    parser = argparse.ArgumentParser(description="PyTorch Classification Training", add_help=add_help)

    parser.add_argument("--data-path", default="/datasets01/imagenet_full_size/061417/", type=str, help="dataset path")
    parser.add_argument('--dataset', default='folder', help='dataset')
    parser.add_argument('--annotation-prefix', default='instances', help='annotation-prefix')
    parser.add_argument("--model", default="resnet18", type=str, help="model name")
    parser.add_argument("--device", default="cuda", type=str, help="device (Use cuda or cpu Default: cuda)")
    parser.add_argument('--gpus', default=1, type=int, help='number of gpus')
    parser.add_argument(
        "-b", "--batch-size", default=32, type=int, help="images per gpu, the total batch size is $NGPU x batch_size"
    )
    parser.add_argument("--epochs", default=90, type=int, metavar="N", help="number of total epochs to run")
    parser.add_argument(
        "-j", "--workers", default=16, type=int, metavar="N", help="number of data loading workers (default: 16)"
    )
    parser.add_argument("--opt", default="sgd", type=str, help="optimizer")
    parser.add_argument("--lr", default=0.1, type=float, help="initial learning rate")
    parser.add_argument("--momentum", default=0.9, type=float, metavar="M", help="momentum")
    parser.add_argument(
        "--wd",
        "--weight-decay",
        default=1e-4,
        type=float,
        metavar="W",
        help="weight decay (default: 1e-4)",
        dest="weight_decay",
    )
    parser.add_argument(
        "--norm-weight-decay",
        default=None,
        type=float,
        help="weight decay for Normalization layers (default: None, same value as --wd)",
    )
    parser.add_argument(
        "--bias-weight-decay",
        default=None,
        type=float,
        help="weight decay for bias parameters of all layers (default: None, same value as --wd)",
    )
    parser.add_argument(
        "--transformer-embedding-decay",
        default=None,
        type=float,
        help="weight decay for embedding parameters for vision transformer models (default: None, same value as --wd)",
    )
    parser.add_argument(
        "--label-smoothing", default=0.0, type=float, help="label smoothing (default: 0.0)", dest="label_smoothing"
    )
    parser.add_argument("--mixup-alpha", default=0.0, type=float, help="mixup alpha (default: 0.0)")
    parser.add_argument("--cutmix-alpha", default=0.0, type=float, help="cutmix alpha (default: 0.0)")
    parser.add_argument("--lr-scheduler", default="steplr", type=str, help="the lr scheduler (default: steplr)")
    parser.add_argument("--lr-warmup-epochs", default=0, type=int, help="the number of epochs to warmup (default: 0)")
    parser.add_argument(
        "--lr-warmup-method", default="constant", type=str, help="the warmup method (default: constant)"
    )
    parser.add_argument("--lr-warmup-decay", default=0.01, type=float, help="the decay for lr")
    parser.add_argument("--lr-step-size", default=30, type=int, help="decrease lr every step-size epochs")
    parser.add_argument("--lr-gamma", default=0.1, type=float, help="decrease lr by a factor of lr-gamma")
    parser.add_argument("--lr-min", default=0.0, type=float, help="minimum lr of lr schedule (default: 0.0)")
    parser.add_argument("--print-freq", default=100, type=int, help="print frequency")
    parser.add_argument("--output-dir", default=".", type=str, help="path to save outputs")
    parser.add_argument("--resume", default="", type=str, help="path of checkpoint")
    parser.add_argument("--start-epoch", default=0, type=int, metavar="N", help="start epoch")
    parser.add_argument(
        "--cache-dataset",
        dest="cache_dataset",
        help="Cache the datasets for quicker initialization. It also serializes the transforms",
        action="store_true",
    )
    parser.add_argument(
        "--sync-bn",
        dest="sync_bn",
        help="Use sync batch norm",
        action="store_true",
    )
    parser.add_argument(
        "--test-only",
        dest="test_only",
        help="Only test the model",
        action="store_true",
    )
    parser.add_argument("--auto-augment", default=None, type=str, help="auto augment policy (default: None)")
    parser.add_argument("--ra-magnitude", default=9, type=int, help="magnitude of auto augment policy")
    parser.add_argument("--augmix-severity", default=3, type=int, help="severity of augmix policy")
    parser.add_argument("--random-erase", default=0.0, type=float, help="random erasing probability (default: 0.0)")

    # Mixed precision training parameters
    parser.add_argument("--amp", action="store_true", help="Use torch.cuda.amp for mixed precision training")

    # distributed training parameters
    parser.add_argument("--world-size", default=1, type=int, help="number of distributed processes")
    parser.add_argument("--dist-url", default="env://", type=str, help="url used to set up distributed training")
    parser.add_argument("--distributed", default=0, type=int,
                        help="use dstributed training even if this script is not launched using torch.disctibuted.launch or run")
    parser.add_argument("--parallel", default=0, type=int, help="can use data parallel mode with distributed is not used")

    parser.add_argument(
        "--model-ema", action="store_true", help="enable tracking Exponential Moving Average of model parameters"
    )
    parser.add_argument(
        "--model-ema-steps",
        type=int,
        default=32,
        help="the number of iterations that controls how often to update the EMA model (default: 32)",
    )
    parser.add_argument(
        "--model-ema-decay",
        type=float,
        default=0.99998,
        help="decay factor for Exponential Moving Average of model parameters (default: 0.99998)",
    )
    parser.add_argument(
        "--use-deterministic-algorithms", action="store_true", help="Forces the use of deterministic algorithms only."
    )
    parser.add_argument(
        "--interpolation", default="bilinear", type=str, help="the interpolation method (default: bilinear)"
    )
    parser.add_argument(
        "--val-resize-size", default=256, type=int, help="the resize size used for validation (default: 256)"
    )
    parser.add_argument(
        "--val-crop-size", default=224, type=int, help="the central crop size used for validation (default: 224)"
    )
    parser.add_argument(
        "--train-crop-size", default=224, type=int, help="the random crop size used for training (default: 224)"
    )
    parser.add_argument("--clip-grad-norm", default=None, type=float, help="the maximum gradient norm (default None)")
    parser.add_argument("--ra-sampler", action="store_true", help="whether to use Repeated Augmentation in training")
    parser.add_argument(
        "--ra-reps", default=3, type=int, help="number of repetitions for Repeated Augmentation (default: 3)"
    )
    parser.add_argument("--weights", default=None, type=str, help="the weights enum name to load")
    parser.add_argument("--weights-state-dict-name", default="model", type=str, help="the weights member name to load from the checkpoint")

    # options to create faster models
    parser.add_argument("--model-surgery", "--lite-model", default=0, type=int, choices=edgeai_torchmodelopt.xmodelopt.surgery.SyrgeryVersion.get_choices(), help="model surgery to create lite models")

    parser.add_argument("--quantization", "--quantize", dest="quantization", default=0, type=int, choices=edgeai_torchmodelopt.xmodelopt.quantization.QuantizationVersion.get_choices(), help="Quaantization Aware Training (QAT)")
    parser.add_argument("--quantization-type", default=None, help="Actual Quaantization Flavour - applies only if quantization is enabled")

    parser.add_argument("--pruning", default=0, type=int, help="Pruning/Sparsity")
    parser.add_argument("--pruning-ratio", default=0.640625, type=float, help="Pruning/Sparsity Factor - applies only if pruning is enabled")
    parser.add_argument("--pruning-type", default='channel', type=str, help="Pruning/Sparsity Type - applies only of pruning is enabled, (options: channel(default), n2m, prunechannelunstructured)")
    parser.add_argument("--pruning-class", default='blend', type=str, help="pruning parametrization class to be used. (options: blend(default), sigmoid, incremental)")
    parser.add_argument("--pruning-global", default=0, type=int, help="Whether to do global pruning (Only supported by unstructured and channel pruning)")
    parser.add_argument("--pruning-init-train-ep", default=5, type=int, help="epochs to train for before starting the pruning")
    parser.add_argument("--pruning-m", type=int, help="The value of m in n:m pruning. Used only in case of n:m pruning")
    
    parser.add_argument("--compile-model", default=0, type=int, help="Compile the model using PyTorch2.0 functionality")
    parser.add_argument("--opset-version", default=18, type=int, help="ONNX Opset version")
    parser.add_argument("--train-epoch-size-factor", default=0.0, type=float,
                        help="Training validation breaks after one iteration - for quick experimentation")
    parser.add_argument("--val-epoch-size-factor", default=0.0, type=float,
                        help="Training validation breaks after one iteration - for quick experimentation")
    return parser


if __name__ == "__main__":
    args = get_args_parser().parse_args()
    main(args)
