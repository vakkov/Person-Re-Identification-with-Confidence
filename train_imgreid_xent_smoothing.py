from __future__ import print_function
from __future__ import division

import os
import sys
import time
import datetime
import argparse
import os.path as osp
import numpy as np

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.optim import lr_scheduler

from torchreid import data_manager
from torchreid.dataset_loader_custom import ImageDataset
from torchreid import transforms as T
from torchreid import models
from torchreid.losses import CrossEntropyLabelSmooth, DeepSupervision, DeepSupervisionAdaptive,AdaptiveLabelSmooth,LabelSmooth_sigmoid,AdaptiveLabelSmooth_sigmoid,modifiedBCE
from torchreid.utils.iotools import save_checkpoint, check_isfile
from torchreid.utils.avgmeter import AverageMeter
from torchreid.utils.logger import Logger
from torchreid.utils.torchtools import set_bn_to_eval, count_num_param
from torchreid.utils.reidtools import visualize_ranked_results, plot_deltaTheta
from torchreid.eval_metrics import evaluate
from torchreid.optimizers import init_optim

from tensorboardX import SummaryWriter
import random
import pdb

parser = argparse.ArgumentParser(description='Train image model with cross entropy loss')
# Datasets
parser.add_argument('--root', type=str, default='data',
                    help="root path to data directory")
parser.add_argument('-d', '--dataset', type=str, default='market1501',
                    choices=data_manager.get_names())
parser.add_argument('-j', '--workers', default=4, type=int,
                    help="number of data loading workers (default: 4)")
parser.add_argument('--height', type=int, default=256,
                    help="height of an image (default: 256)")
parser.add_argument('--width', type=int, default=128,
                    help="width of an image (default: 128)")
parser.add_argument('--split-id', type=int, default=0,
                    help="split index (0-based)")
# CUHK03-specific setting
parser.add_argument('--cuhk03-labeled', action='store_true',
                    help="use labeled images, if false, detected images are used (default: False)")
parser.add_argument('--cuhk03-classic-split', action='store_true',
                    help="use classic split by Li et al. CVPR'14 (default: False)")
parser.add_argument('--use-metric-cuhk03', action='store_true',
                    help="use cuhk03-metric (default: False)")
# Optimization options
parser.add_argument('--optim', type=str, default='adam',
                    help="optimization algorithm (see optimizers.py)")
parser.add_argument('--max-epoch', default=60, type=int,
                    help="maximum epochs to run")
parser.add_argument('--start-epoch', default=0, type=int,
                    help="manual epoch number (useful on restarts)")
parser.add_argument('--train-batch', default=32, type=int,
                    help="train batch size")
parser.add_argument('--test-batch', default=100, type=int,
                    help="test batch size")
parser.add_argument('--lr', '--learning-rate', default=0.0003, type=float,
                    help="initial learning rate")
parser.add_argument('--stepsize', default=[20, 40], nargs='+', type=int,
                    help="stepsize to decay learning rate")
parser.add_argument('--gamma', default=0.1, type=float,
                    help="learning rate decay")
parser.add_argument('--weight-decay', default=5e-04, type=float,
                    help="weight decay (default: 5e-04)")
parser.add_argument('--fixbase-epoch', default=0, type=int,
                    help="epochs to fix base network (only train classifier, default: 0)")
parser.add_argument('--fixbase-lr', default=0.0003, type=float,
                    help="learning rate (when base network is frozen)")
parser.add_argument('--freeze-bn', action='store_true',
                    help="freeze running statistics in BatchNorm layers during training (default: False)")
parser.add_argument('--label-smooth', action='store_true',
                    help="use label smoothing regularizer in cross entropy loss")

parser.add_argument('--scheduler', type=int, default=0,
                    help="weight to balance rotation loss")

parser.add_argument('--test-rot', action='store_true',
                    help="Train only classifier to get rotation")
parser.add_argument('--plot-deltaTheta', action='store_true',
                    help="Plot DeltaTheta, only available in evaluation mode (default: False)")


# Architecture
parser.add_argument('-a', '--arch', type=str, default='resnet50', choices=models.get_names())
# Miscs
parser.add_argument('--print-freq', type=int, default=10,
                    help="print frequency")
parser.add_argument('--seed', type=int, default=1,
                    help="manual seed")
parser.add_argument('--resume', type=str, default='', metavar='PATH')
parser.add_argument('--load-weights', type=str, default='',
                    help="load pretrained weights but ignores layers that don't match in size")
parser.add_argument('--evaluate', action='store_true',
                    help="evaluation only")
parser.add_argument('--eval-step', type=int, default=-1,
                    help="run evaluation for every N epochs (set to -1 to test after training)")
parser.add_argument('--start-eval', type=int, default=0,
                    help="start to evaluate after specific epoch")
parser.add_argument('--save-dir', type=str, default='log')
parser.add_argument('--use-cpu', action='store_true',
                    help="use cpu")
parser.add_argument('--gpu-devices', default='0', type=str,
                    help='gpu device ids for CUDA_VISIBLE_DEVICES')
parser.add_argument('--use-avai-gpus', action='store_true',
                    help="use available gpus instead of specified devices (this is useful when using managed clusters)")
parser.add_argument('--visualize-ranks', action='store_true',
                    help="visualize ranked results, only available in evaluation mode (default: False)")

parser.add_argument('--lambda-xent', type=float, default=1,
                    help="weight to balance cross entropy loss")

parser.add_argument('--initial-train', type=int, default=0,
                    help="run initial training using Cross-Entropy (Default: 10)")

parser.add_argument('--use-sigmoid', action='store_true',
                    help="use sigmoid instead of softmax(default: False)")
# global variables
#args = parser.parse_args()
#best_rank1 = -np.inf


def main(args):
    args = parser.parse_args(args)
    #global best_rank1
    best_rank1 = -np.inf
    torch.manual_seed(args.seed)
    # np.random.seed(args.seed)
    # random.seed(args.seed)
    if not args.use_avai_gpus: os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_devices
    use_gpu = torch.cuda.is_available()
    if args.use_cpu: use_gpu = False

    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_train.txt'))
    else:
        test_dir = args.save_dir
        if args.save_dir =='log':
            if args.resume:
                test_dir = os.path.dirname(args.resume)
            else:
                test_dir = os.path.dirname(args.load_weights)
        sys.stdout = Logger(osp.join(test_dir, 'log_test.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    if use_gpu:
        print("Currently using GPU {}".format(args.gpu_devices))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(args.seed)
        # print("Currently using GPU {}".format(args.gpu_devices))
        # #cudnn.benchmark = False
        # cudnn.deterministic = True
        # torch.cuda.manual_seed_all(args.seed)
        # torch.set_default_tensor_type('torch.DoubleTensor')
    else:
        print("Currently using CPU (GPU is highly recommended)")

    print("Initializing dataset {}".format(args.dataset))
    dataset = data_manager.init_imgreid_dataset(
        root=args.root, name=args.dataset, split_id=args.split_id,
        cuhk03_labeled=args.cuhk03_labeled, cuhk03_classic_split=args.cuhk03_classic_split,
    )

    transform_train = T.Compose([
        #T.Random2DTranslation(args.height, args.width),
        T.Resize((args.height, args.width)),
        T.RandomSizedEarser(),
        T.RandomHorizontalFlip_rot(),
        #T.Resize((args.height, args.width)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    transform_test = T.Compose([
        T.Resize((args.height, args.width)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    pin_memory = True if use_gpu else False

    if not (args.dataset == 'market1501' or args.dataset == 'msmt17' or args.dataset == 'dukemtmcreid' or args.dataset == 'cuhk03'):
        trainloader = DataLoader(
            ImageDataset(dataset.train,dataset.root_angle, transform=transform_train),
            batch_size=args.train_batch, shuffle=True, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=True,
        )

        queryloader = DataLoader(
            ImageDataset(dataset.query,dataset.root_angle, transform=transform_test),
            batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=False,
        )

        galleryloader = DataLoader(
            ImageDataset(dataset.gallery,dataset.root_angle, transform=transform_test),
            batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=False,
        )
    else:
        trainloader = DataLoader(
            ImageDataset(dataset.train,-1, transform=transform_train),
            batch_size=args.train_batch, shuffle=True, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=True,
        )

        queryloader = DataLoader(
            ImageDataset(dataset.query,-1, transform=transform_test),
            batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=False,
        )

        galleryloader = DataLoader(
            ImageDataset(dataset.gallery,-1, transform=transform_test),
            batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
            pin_memory=pin_memory, drop_last=False,
        )

    print("Initializing model: {}".format(args.arch))
    model = models.init_model(name=args.arch, num_classes=dataset.num_train_pids, loss={'xent'}, use_gpu=use_gpu)
    print("Model size: {:.3f} M".format(count_num_param(model)))

    criterion = AdaptiveLabelSmooth(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
    if args.use_sigmoid:
        if args.label_smooth:
            print("Using Sigmoid Label Smoothing")
            criterion_simple = LabelSmooth_sigmoid(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
        else:
            criterion_simple = modifiedBCE(use_gpu=use_gpu)
        #criterion = LabelSmooth_sigmoid(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
        criterion = modifiedBCE(use_gpu=use_gpu)
    else:
        if args.label_smooth:
            print("Using Label Smoothing")
            criterion_simple = CrossEntropyLabelSmooth(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
        else:
            criterion_simple = nn.CrossEntropyLoss()
    optimizer = init_optim(args.optim, model.parameters(), args.lr, args.weight_decay)
    if args.scheduler != 0:
        scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=args.stepsize, gamma=args.gamma)

    if args.fixbase_epoch > 0:
        if hasattr(model, 'classifier') and isinstance(model.classifier, nn.Module):
            optimizer_tmp = init_optim(args.optim, model.classifier.parameters(), args.fixbase_lr, args.weight_decay)
        else:
            print("Warn: model has no attribute 'classifier' and fixbase_epoch is reset to 0")
            args.fixbase_epoch = 0

    if args.load_weights and check_isfile(args.load_weights):
        # load pretrained weights but ignore layers that don't match in size
        checkpoint = torch.load(args.load_weights)
        pretrain_dict = checkpoint['state_dict']
        model_dict = model.state_dict()
        pretrain_dict = {k: v for k, v in pretrain_dict.items() if k in model_dict and model_dict[k].size() == v.size()}
        model_dict.update(pretrain_dict)
        model.load_state_dict(model_dict)
        print("Loaded pretrained weights from '{}'".format(args.load_weights))

    if args.resume and check_isfile(args.resume):
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        args.start_epoch = checkpoint['epoch'] + 1
        best_rank1 = checkpoint['rank1']
        print("Loaded checkpoint from '{}'".format(args.resume))
        print("- start_epoch: {}\n- rank1: {}".format(args.start_epoch, best_rank1))

    if use_gpu:
        model = nn.DataParallel(model).cuda()

    if args.evaluate:
        print("Evaluate only")
        test_dir = args.save_dir
        if args.save_dir =='log':
            if args.resume:
                test_dir = os.path.dirname(args.resume)
            else:
                test_dir = os.path.dirname(args.load_weights)
        distmat = test(model, queryloader, galleryloader, use_gpu, args,writer=None,epoch=-1, return_distmat=True, use_cosine = args.plot_deltaTheta)

        if args.visualize_ranks:
            visualize_ranked_results(
                distmat, dataset,
                save_dir=osp.join(test_dir, 'ranked_results'),
                topk=10,
            )
        if args.plot_deltaTheta:
            plot_deltaTheta(distmat, dataset,save_dir=osp.join(test_dir,'deltaTheta_results'), min_rank=1)
        return


    writer = SummaryWriter(log_dir=osp.join(args.save_dir, 'tensorboard'))
    start_time = time.time()
    train_time = 0
    best_epoch = args.start_epoch
    print("==> Start training")

    if args.test_rot:
        print("Training only classifier for rotation")
        model = models.init_model(name='rot_tester',base_model=model,inplanes=2048,num_rot_classes = 8)
        criterion_rot = nn.CrossEntropyLoss()
        optimizer_rot = init_optim(args.optim, model.fc_rot.parameters(), args.fixbase_lr, args.weight_decay)
        if use_gpu:
            model = nn.DataParallel(model).cuda()
        try:
            best_epoch = 0
            for epoch in range(0, args.max_epoch):
                start_train_time = time.time()
                train_rotTester(epoch, model, criterion_rot, optimizer_rot, trainloader, use_gpu, writer,args)
                train_time += round(time.time() - start_train_time)

                if args.scheduler != 0:
                    scheduler.step()

                if (epoch + 1) > args.start_eval and args.eval_step > 0 and (epoch + 1) % args.eval_step == 0 or (epoch + 1) == args.max_epoch:
                    if (epoch + 1) == args.max_epoch:
                        if use_gpu:
                            state_dict = model.module.state_dict()
                        else:
                            state_dict = model.state_dict()

                        save_checkpoint({
                            'state_dict': state_dict,
                            'rank1': -1,
                            'epoch': epoch,
                        }, False, osp.join(args.save_dir, 'beforeTesting_checkpoint_ep' + str(epoch + 1) + '.pth.tar'))
                    print("==> Test")
                    rank1 = test_rotTester(model,criterion_rot,queryloader, galleryloader, trainloader, use_gpu,args,writer=writer,epoch=epoch)
                    is_best = rank1 > best_rank1

                    if is_best:
                        best_rank1 = rank1
                        best_epoch = epoch + 1

                    if use_gpu:
                        state_dict = model.module.state_dict()
                    else:
                        state_dict = model.state_dict()

                    save_checkpoint({
                        'state_dict': state_dict,
                        'rank1': rank1,
                        'epoch': epoch,
                    }, is_best, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch + 1) + '.pth.tar'))

            print("==> Best Cccuracy {:.1%}, achieved at epoch {}".format(best_rank1, best_epoch))

            elapsed = round(time.time() - start_time)
            elapsed = str(datetime.timedelta(seconds=elapsed))
            train_time = str(datetime.timedelta(seconds=train_time))
            print("Finished. Total elapsed time (h:m:s): {}. Training time (h:m:s): {}.".format(elapsed, train_time))
            return best_rank1, best_epoch
        except KeyboardInterrupt:
            if use_gpu:
                state_dict = model.module.state_dict()
            else:
                state_dict = model.state_dict()

            save_checkpoint({
                'state_dict': state_dict,
                'rank1': -1,
                'epoch': epoch,
            }, False, osp.join(args.save_dir, 'keyboardInterrupt_checkpoint_ep' + str(epoch + 1) + '.pth.tar'))

        return None, None

    if args.fixbase_epoch > 0:
        print("Train classifier for {} epochs while keeping base network frozen".format(args.fixbase_epoch))

        for epoch in range(args.fixbase_epoch):
            start_train_time = time.time()
            train(epoch, model, criterion_simple, optimizer_tmp, trainloader, use_gpu,writer, args, freeze_bn=True)
            train_time += round(time.time() - start_train_time)

        del optimizer_tmp
        print("Now open all layers for training")
    best_epoch = 0
    for epoch in range(args.start_epoch, args.max_epoch):
        start_train_time = time.time()
        if epoch < args.initial_train:
            train(epoch, model, criterion_simple, optimizer, trainloader, use_gpu,writer, args)
        else:
            train(epoch, model, criterion, optimizer, trainloader, use_gpu,writer, args)
        train_time += round(time.time() - start_train_time)

        if args.scheduler != 0:
            scheduler.step()


        if (epoch + 1) > args.start_eval and args.eval_step > 0 and (epoch + 1) % args.eval_step == 0 or (epoch + 1) == args.max_epoch:
            if (epoch + 1) == args.max_epoch:
                if use_gpu:
                    state_dict = model.module.state_dict()
                else:
                    state_dict = model.state_dict()

                save_checkpoint({
                    'state_dict': state_dict,
                    'rank1': -1,
                    'epoch': epoch,
                }, False, osp.join(args.save_dir, 'beforeTesting_checkpoint_ep' + str(epoch + 1) + '.pth.tar'))
            print("==> Test")

            rank1 = test(model, queryloader, galleryloader, use_gpu, args,writer=writer,epoch=epoch)

            is_best = rank1 > best_rank1

            if is_best:
                best_rank1 = rank1
                best_epoch = epoch + 1

            if use_gpu:
                state_dict = model.module.state_dict()
            else:
                state_dict = model.state_dict()

            save_checkpoint({
                'state_dict': state_dict,
                'rank1': rank1,
                'epoch': epoch,
            }, is_best, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch + 1) + '.pth.tar'))

    print("==> Best Rank-1 {:.1%}, achieved at epoch {}".format(best_rank1, best_epoch))

    elapsed = round(time.time() - start_time)
    elapsed = str(datetime.timedelta(seconds=elapsed))
    train_time = str(datetime.timedelta(seconds=train_time))
    print("Finished. Total elapsed time (h:m:s): {}. Training time (h:m:s): {}.".format(elapsed, train_time))
    return best_rank1, best_epoch

def train_rotTester(epoch, model, criterion_rot,optimizer,trainloader,use_gpu, writer,args,freeze_bn=True):

    #pdb.set_trace()
    losses = AverageMeter()
    batch_time = AverageMeter()
    data_time = AverageMeter()
    rot_loss_meter = AverageMeter()
    printed = False
    model.train()
    if freeze_bn or args.freeze_bn:
        model.apply(set_bn_to_eval)
        #model.base_model.eval()
    end = time.time()
    for batch_idx, (imgs, pids, rotation_labels) in enumerate(trainloader):
        data_time.update(time.time() - end)
        if use_gpu:
            imgs, pids,rotation_labels = imgs.cuda(), pids.cuda(), rotation_labels.cuda()

        rotation_logits = model(imgs)
        rot_loss = criterion_rot(rotation_logits, rotation_labels)

        loss = rot_loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        batch_time.update(time.time() - end)

        losses.update(loss.item(), pids.size(0))

        rot_loss_meter.update(rot_loss.item(), pids.size(0))

        if (batch_idx + 1) % args.print_freq == 0:
            if not printed:
              printed = True
            else:
              # Clean the current line
              sys.stdout.console.write("\033[F\033[K")
              #sys.stdout.console.write("\033[K")
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.4f} ({data_time.avg:.4f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Rot Loss {rot_loss.val:.4f} ({rot_loss.avg:.4f})\t'.format(
                   epoch + 1, batch_idx + 1, len(trainloader), batch_time=batch_time,
                   data_time=data_time, loss=losses, rot_loss=rot_loss_meter))

        end = time.time()
    writer.add_scalars(
      'loss',
      dict(angle_loss = rot_loss_meter.avg,
           loss=losses.avg),
      epoch + 1)

def train(epoch, model, criterion, optimizer, trainloader, use_gpu,writer, args,freeze_bn=False):
    losses = AverageMeter()
    xent_losses = AverageMeter()
    batch_time = AverageMeter()
    data_time = AverageMeter()
    printed = False
    model.train()

    if freeze_bn or args.freeze_bn:
        model.apply(set_bn_to_eval)

    end = time.time()
    for batch_idx, (imgs, pids, _) in enumerate(trainloader):
        data_time.update(time.time() - end)

        if use_gpu:
            imgs, pids = imgs.cuda(), pids.cuda()

        outputs, epsilon = model(imgs)
        if not (isinstance(criterion, AdaptiveLabelSmooth) or isinstance(criterion, AdaptiveLabelSmooth_sigmoid)):
            if isinstance(outputs, tuple):
                xent_loss = DeepSupervision(criterion, outputs, pids)
            else:
                xent_loss = criterion(outputs, pids)
        else:
            if isinstance(outputs, tuple):
                xent_loss = DeepSupervisionAdaptive(criterion, outputs, pids,epsilon)
            else:
                xent_loss = criterion(outputs, pids,epsilon)
        loss = args.lambda_xent *xent_loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)

        losses.update(loss.item(), pids.size(0))
        xent_losses.update(xent_loss.item(), pids.size(0))
        if (batch_idx + 1) % args.print_freq == 0:
            if not printed:
              printed = True
            else:
              # Clean the current line
              sys.stdout.console.write("\033[F\033[K")
              #sys.stdout.console.write("\033[K")
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.4f} ({data_time.avg:.4f})\t'
                  'epsilon {epsilon} \t'
                  'Xent_Loss {xent_loss.val:.4f} ({xent_loss.avg:.4f})\t'
                  'Total_Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                   epoch + 1, batch_idx + 1, len(trainloader), batch_time=batch_time,
                   data_time=data_time,epsilon=epsilon.data.cpu().squeeze()[0],xent_loss=xent_losses, loss=losses))

        end = time.time()

    writer.add_scalars(
      'loss',
      dict(loss=losses.avg),
      epoch + 1)

def test_rotTester(model,criterion_rot,queryloader, galleryloader, trainloader, use_gpu,args,writer,epoch, ranks=[1, 5, 10, 20], return_distmat=False):
    batch_time = AverageMeter()
    top1_test = AverageMeter()
    top1_train = AverageMeter()
    rot_loss_meter = AverageMeter()
    training_rot_loss_meter = AverageMeter()

    model.eval()

    with torch.no_grad():

        for batch_idx, (imgs, pids, rotation_labels) in enumerate(queryloader):
            if use_gpu: imgs,rotation_labels = imgs.cuda(),rotation_labels.cuda()

            end = time.time()
            rot_logits = model(imgs)
            batch_time.update(time.time() - end)
            rot_loss = criterion_rot(rot_logits, rotation_labels)
            prec1 = accuracy(rot_logits.data, rotation_labels.data)
            top1_test.update(prec1[0])
            rot_loss_meter.update(rot_loss.item(), pids.size(0))
        end = time.time()

        print("--------Done Query-----")
        for batch_idx, (imgs, pids, rotation_labels) in enumerate(galleryloader):
            if use_gpu: imgs,rotation_labels = imgs.cuda(),rotation_labels.cuda()

            end = time.time()
            rot_logits = model(imgs)
            batch_time.update(time.time() - end)

            prec1 = accuracy(rot_logits.data, rotation_labels.data)
            top1_test.update(prec1[0])
            rot_loss_meter.update(rot_loss.item(), pids.size(0))

        print("--------Done Gallery-----")
        for batch_idx, (imgs, pids, rotation_labels) in enumerate(trainloader):
            if use_gpu: imgs,rotation_labels = imgs.cuda(),rotation_labels.cuda()

            end = time.time()
            rot_logits = model(imgs)
            batch_time.update(time.time() - end)

            prec1 = accuracy(rot_logits.data, rotation_labels.data)
            top1_train.update(prec1[0])
            training_rot_loss_meter.update(rot_loss.item(), pids.size(0))

        print("--------Done Training-----")
    print("==> BatchTime(s)/BatchSize(img): {:.3f}/{}".format(batch_time.avg, args.test_batch))

    print("Test Angle Acc:{:.2f}".format(top1_test.avg.cpu().numpy()[0]))
    print("Train Angle Acc:{:.2f}".format(top1_train.avg.cpu().numpy()[0]))
    print("------------------")

    if writer != None:
        writer.add_scalars(
          'Accuracy Graph',
          dict(test_accuracy=top1_test.avg.cpu().numpy()[0],
               train_accuracy = top1_train.avg.cpu().numpy()[0]),
          epoch + 1)

        writer.add_scalars(
        'Loss Graph',
        dict(test_loss=rot_loss_meter.avg,
             train_loss = training_rot_loss_meter.avg),
        epoch + 1)
    return top1_test.avg.cpu().numpy()[0]

def test(model, queryloader, galleryloader, use_gpu, args,writer,epoch, ranks=[1, 5, 10, 20], return_distmat=False,use_cosine = False):

    batch_time = AverageMeter()

    model.eval()

    with torch.no_grad():
        qf, q_pids, q_camids = [], [], []
        for batch_idx, (imgs, pids, camids) in enumerate(queryloader):
            if use_gpu: imgs = imgs.cuda()

            end = time.time()
            features = model(imgs)
            batch_time.update(time.time() - end)

            features = features.data.cpu()
            qf.append(features)
            q_pids.extend(pids)
            q_camids.extend(camids)
        qf = torch.cat(qf, 0)
        q_pids = np.asarray(q_pids)
        q_camids = np.asarray(q_camids)

        print("Extracted features for query set, obtained {}-by-{} matrix".format(qf.size(0), qf.size(1)))

        gf, g_pids, g_camids = [], [], []
        end = time.time()
        for batch_idx, (imgs, pids, camids) in enumerate(galleryloader):
            if use_gpu: imgs = imgs.cuda()

            end = time.time()
            features = model(imgs)
            batch_time.update(time.time() - end)

            features = features.data.cpu()
            gf.append(features)
            g_pids.extend(pids)
            g_camids.extend(camids)
        gf = torch.cat(gf, 0)
        g_pids = np.asarray(g_pids)
        g_camids = np.asarray(g_camids)

        print("Extracted features for gallery set, obtained {}-by-{} matrix".format(gf.size(0), gf.size(1)))

    print("==> BatchTime(s)/BatchSize(img): {:.3f}/{}".format(batch_time.avg, args.test_batch))

    if not use_cosine:
        m, n = qf.size(0), gf.size(0)
        distmat = torch.pow(qf, 2).sum(dim=1, keepdim=True).expand(m, n) + \
                  torch.pow(gf, 2).sum(dim=1, keepdim=True).expand(n, m).t()
        distmat.addmm_(1, -2, qf, gf.t())
        distmat = distmat.numpy()
    else:
        m, n = qf.size(0), gf.size(0)
        qf_norm = qf/qf.norm(dim=1)[:,None]
        gf_norm = gf/gf.norm(dim=1)[:,None]
        distmat = torch.addmm(1,torch.ones((m,n)),-1,qf_norm,gf_norm.transpose(0,1))
        distmat = distmat.numpy()

    print("Computing CMC and mAP")
    cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids, use_metric_cuhk03=args.use_metric_cuhk03)

    print("Results ----------")
    print("mAP: {:.1%}".format(mAP))
    print("CMC curve")
    for r in ranks:
        print("Rank-{:<3}: {:.1%}".format(r, cmc[r-1]))
    print("------------------")

    if return_distmat:
        return distmat

    if writer != None:
        writer.add_scalars(
          'Testing',
          dict(rank_1=cmc[0],
               rank_5 = cmc[4],
               mAP=mAP),
          epoch + 1)
    return cmc[0]

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

if __name__ == '__main__':
    main(sys.argv[1:])
