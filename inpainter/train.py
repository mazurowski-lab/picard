##############################
# train the pluralistic image completion network.
# Modified from https://github.com/daa233/generative-inpainting-pytorch/blob/master/train.py
##############################

import os
import numpy as np
import random
import time
import shutil
from argparse import ArgumentParser

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torchvision.utils as vutils
from tensorboardX import SummaryWriter

from trainer import Trainer
from data.dataset import Dataset
from inpainterutils.tools import get_config, random_bbox, mask_image, log_startup_info
from inpainterutils.logger import get_logger

parser = ArgumentParser()
parser.add_argument('--config', type=str, default='configs/config.yaml',
                    help="training configuration")
parser.add_argument('--seed', type=int, help='manual seed')
parser.add_argument('--print_net', action='store_true', help='show net architecture/layers?')
parser.add_argument('--print_gpu_info', action='store_true', help='print info of GPUs being used?')

def main():
    args = parser.parse_args()
    config = get_config(args.config)

    torch.autograd.set_detect_anomaly(True)

    # CUDA configuration
    cuda = config['cuda']
    device_ids = config['gpu_ids']
    if cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(str(i) for i in device_ids)
        #device_ids = list(range(len(device_ids)))
        config['gpu_ids'] = device_ids
        cudnn.benchmark = True

    # Configure checkpoint path
    checkpoint_path = os.path.join('checkpoints',
                                   config['dataset_name'],
                                   config['train']['mask_type'] + '_' + config['expname'])
    if not os.path.exists(checkpoint_path):
        os.makedirs(checkpoint_path)
    shutil.copy(args.config, os.path.join(checkpoint_path, os.path.basename(args.config)))
    writer = SummaryWriter(logdir=checkpoint_path)
    logger = get_logger(checkpoint_path)    # get logger and configure it at the first call

    logger.info("Arguments: {}".format(args))
    # Set random seed
    if args.seed is None:
        args.seed = random.randint(1, 10000)
    logger.info("Random seed: {}".format(args.seed))
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if cuda:
        torch.cuda.manual_seed_all(args.seed)

    # Log the configuration
    logger.info("Configuration: {}".format(config))

    try:  # for unexpected error logging
        # Load the dataset
        logger.info("Training on dataset: {}".format(config['dataset_name']))
        train_dataset = Dataset(config=config,
                                data_path=config['train_data_path'],
                                with_subfolder=config['data_with_subfolder'],
                                image_shape=config['train']['image_shape'],
                                random_crop=config['train']['random_crop'],
                                subset_frac=config['train']['subset_frac'] 
                                )

        niter = log_startup_info(len(train_dataset), config)

        train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                                   batch_size=config['train']['batch_size'],
                                                   shuffle=True,
                                                   num_workers=config['num_workers'])

        # Define the trainer
        #print(config)
        trainer = Trainer(config)
        if args.print_net:
            logger.info("\n{}".format(trainer.netG))
            logger.info("\n{}".format(trainer.localD))
            logger.info("\n{}".format(trainer.globalD))

        if cuda:
            trainer = nn.parallel.DataParallel(trainer, device_ids=list(range(len(device_ids))))
            trainer_module = trainer.module
        else:
            trainer_module = trainer

        
        
        # Get the resume iteration to restart training
        start_iteration = trainer_module.resume(config['train']['resume']) if config['train']['resume'] else 1

        iterable_train_loader = iter(train_loader)

        time_count = time.time()


       # reporter = MemReporter(trainer)
        estimate_total_train_time = True

        for iteration in range(start_iteration, niter + 1):
            try:
                ground_truth = next(iterable_train_loader)
            except StopIteration:
                iterable_train_loader = iter(train_loader)
                ground_truth = next(iterable_train_loader)


            # Prepare the inputs
            bboxes = random_bbox(config, batch_size=ground_truth.size(0))
            x, mask = mask_image(ground_truth, bboxes, config)
            if cuda:
                x = x.cuda()
                mask = mask.cuda()
                ground_truth = ground_truth.cuda()

            ###### Forward pass ######
            compute_g_loss = iteration % config['train']['n_critic'] == 0
            losses, inpainted_result, offset_flow = trainer(x, bboxes, mask, ground_truth, compute_g_loss)
            # Scalars from different devices are gathered into vectors
            for k in losses.keys():
                if not losses[k].dim() == 0:
                    losses[k] = torch.mean(losses[k])
            
            ###### Backward pass ######
            # Update D
            trainer_module.optimizer_d.zero_grad()
            losses['d'] = losses['wgan_d'] + losses['wgan_gp'] * config['wgan_gp_lambda']
            losses['d'].backward() 

            # Update G
            if compute_g_loss:
                trainer_module.optimizer_g.zero_grad()
                losses['g'] = losses['l1'] * config['l1_loss_alpha'] \
                              + losses['ae'] * config['ae_loss_alpha'] \
                              + losses['wgan_g'] * config['gan_loss_alpha']
                losses['g'].backward()
                trainer_module.optimizer_g.step()
            trainer_module.optimizer_d.step()

            # Log and visualization
            log_losses = ['l1', 'ae', 'wgan_g', 'wgan_d', 'wgan_gp', 'g', 'd']

            if iteration % config['train']['print_iter'] == 0:
                time_count = time.time() - time_count
                speed = config['train']['print_iter'] / time_count
                speed_msg = 'speed: %.2f batches/s ' % speed
                if estimate_total_train_time: 
                    total_train_time_days = (niter/iteration) * time_count / 86400
                    speed_msg += '\t\t TOTAL ESTIMATED TRAINING TIME = {} days'.format(total_train_time_days)
                    estimate_total_train_time = False
                time_count = time.time()

                message = 'Iter: [%d/%d] ' % (iteration, niter)
                for k in log_losses:
                    v = losses.get(k, 0.)
                    writer.add_scalar(k, v, iteration)
                    message += '%s: %.6f ' % (k, v)
                message += speed_msg
                logger.info(message)


            if iteration % (config['train']['viz_iter']) == 0:
                viz_max_out = config['train']['viz_max_out']

                if x.size(0) > viz_max_out:
                    viz_images = torch.stack([x[:viz_max_out], inpainted_result[:viz_max_out]], dim=1)
                else:
                    viz_images = torch.stack([x, inpainted_result], dim=1)
                viz_images = viz_images.view(-1, *list(x.size())[1:])
                vutils.save_image(viz_images,
                                  '%s/niter_%03d.png' % (checkpoint_path, iteration),
                                  nrow=3 * 3,
                                  normalize=True)

            # Save the model
            if iteration % config['train']['snapshot_save_iter'] == 0:
                trainer_module.save_model(checkpoint_path, iteration)

    except Exception as e:  # for unexpected error logging
        logger.error("{}".format(e))
        raise e

if __name__ == '__main__':
    main()
