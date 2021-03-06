# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import numpy as np
import pprint
import time
import _init_paths

import torch

from torch.autograd import Variable
import pickle
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
# from model.nms.nms_wrapper import nms
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv
#from model.utils.net_utils import save_net, load_net, vis_detections
from model.utils.parser_func import parse_args,set_dataset_args
from lib.datasets.gas_real import iou_thresh

import pdb

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3

class Timer(object):
    """A simple timer."""
    def __init__(self):
        self.total_time = 0.
        self.calls = 0
        self.start_time = 0.
        self.diff = 0.
        self.average_time = 0.

    def tic(self):
        # using time.time instead of time.clock because time time.clock
        # does not normalize for multithreading
        self.start_time = time.time()

    def toc(self, average=True):
        self.diff = time.time() - self.start_time
        self.total_time += self.diff
        self.calls += 1
        self.average_time = self.total_time / self.calls
        if average:
            return self.average_time
        else:
            return self.diff


lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

print("iou_thresh={}".format(iou_thresh))

args = parse_args()

print('Called with args:')
print(args)
args = set_dataset_args(args,test=True)


def test_one_weight(weight_name=None):
  if torch.cuda.is_available() and not args.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")
  np.random.seed(cfg.RNG_SEED)

  if args.cfg_file is not None:
    cfg_from_file(args.cfg_file)
  if args.set_cfgs_target is not None:
    cfg_from_list(args.set_cfgs_target)

  # print('Using config:')
  # pprint.pprint(cfg)

  cfg.TRAIN.USE_FLIPPED = False
  imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name_target, False)#############################################################################
  # imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbtest_name_target, False)
  imdb.competition_mode(on=True)

  print('{:d} roidb entries'.format(len(roidb)))

  # initilize the network here.
  from model.faster_rcnn.vgg16_HTCN import vgg16
  from model.faster_rcnn.resnet_HTCN import resnet

  if args.net == 'vgg16':
      fasterRCNN = vgg16(imdb.classes, pretrained=True, class_agnostic=args.class_agnostic,lc=args.lc,gc=args.gc, la_attention=args.LA_ATT,
                         mid_attention = args.MID_ATT)
  elif args.net == 'res101':
      fasterRCNN = resnet(imdb.classes, 101, pretrained=True, class_agnostic=args.class_agnostic,
                          lc=args.lc, gc=args.gc, la_attention=args.LA_ATT,
                          mid_attention = args.MID_ATT)
  #elif args.net == 'res50':
  #  fasterRCNN = resnet(imdb.classes, 50, pretrained=True, class_agnostic=args.class_agnostic,context=args.context)

  else:
    print("network is not defined")
    pdb.set_trace()

  fasterRCNN.create_architecture()

  if weight_name is None:
    print("load checkpoint %s" % (args.load_name))
    checkpoint = torch.load(args.load_name)
  else:
    print("load checkpoint %s" % (weight_name))
    checkpoint = torch.load(weight_name)
  fasterRCNN.load_state_dict(checkpoint['model'])
  if 'pooling_mode' in checkpoint.keys():
    cfg.POOLING_MODE = checkpoint['pooling_mode']


  print('load model successfully!')
  # initilize the tensor holder here.
  im_data = torch.FloatTensor(1)
  im_info = torch.FloatTensor(1)
  num_boxes = torch.LongTensor(1)
  gt_boxes = torch.FloatTensor(1)

  # ship to cuda
  if args.cuda:
    im_data = im_data.cuda()
    im_info = im_info.cuda()
    num_boxes = num_boxes.cuda()
    gt_boxes = gt_boxes.cuda()

  # make variable
  im_data = Variable(im_data)
  im_info = Variable(im_info)
  num_boxes = Variable(num_boxes)
  gt_boxes = Variable(gt_boxes)

  if args.cuda:
    cfg.CUDA = True

  if args.cuda:
    fasterRCNN.cuda()

  start = time.time()
  max_per_image = 100

  thresh = 0.0


  # save_name = args.load_name.split('/')[-1]
  save_name = args.log_ckpt_name + '_test_in_'+ args.dataset_t
  num_images = len(imdb.image_index)
  all_boxes = [[[] for _ in xrange(num_images)]
               for _ in xrange(imdb.num_classes)]

  output_dir = get_output_dir(imdb, save_name)
  dataset = roibatchLoader(roidb, ratio_list, ratio_index, 1, \
                        # imdb.num_classes, training=False, normalize = False, path_return=True)
                        imdb.num_classes, training = False, normalize = False)
  dataloader = torch.utils.data.DataLoader(dataset, batch_size=1,
                            shuffle=False, num_workers=0,
                            pin_memory=True)

  data_iter = iter(dataloader)

  _t = {'im_detect': time.time(), 'misc': time.time()}
  _t1 = {'im_detect': Timer(), 'misc': Timer()}
  det_file = os.path.join(output_dir, 'detections.pkl')

  fasterRCNN.eval()
  empty_array = np.transpose(np.array([[],[],[],[],[]]), (1,0))
  for i in range(num_images):

      data = next(data_iter)
      im_data.data.resize_(data[0].size()).copy_(data[0])
      #print(data[0].size())
      im_info.data.resize_(data[1].size()).copy_(data[1])
      gt_boxes.data.resize_(data[2].size()).copy_(data[2])
      num_boxes.data.resize_(data[3].size()).copy_(data[3])

      det_tic = time.time()
      _t1['im_detect'].tic()

      rois, cls_prob, bbox_pred, \
      rpn_loss_cls, rpn_loss_box, \
      RCNN_loss_cls, RCNN_loss_bbox, \
      rois_label,d_pred,_,_,_ = fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

      scores = cls_prob.data
      boxes = rois.data[:, :, 1:5]
      # d_pred = d_pred.data
      # path = data[4]

      if cfg.TEST.BBOX_REG:
          # Apply bounding-box regression deltas
          box_deltas = bbox_pred.data
          if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
          # Optionally normalize targets by a precomputed mean and stdev
            if args.class_agnostic:
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                           + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                box_deltas = box_deltas.view(1, -1, 4)
            else:
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                           + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                box_deltas = box_deltas.view(1, -1, 4 * len(imdb.classes))

          pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
          pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
      else:
          # Simply repeat the boxes, once for each class
          pred_boxes = np.tile(boxes, (1, scores.shape[1]))

      pred_boxes /= data[1][0][2].item()

      scores = scores.squeeze() #[1, 300, 2] -> [300, 2]
      pred_boxes = pred_boxes.squeeze() #[1, 300, 8] -> [300, 8]
      det_toc = time.time()
      detect_time = det_toc - det_tic
      misc_tic = time.time()

      for j in xrange(1, imdb.num_classes):
          inds = torch.nonzero(scores[:,j]>thresh).view(-1) #[300]
          # if there is det
          if inds.numel() > 0:
            cls_scores = scores[:,j][inds] #[300]
            _, order = torch.sort(cls_scores, 0, True)
            if args.class_agnostic:
              cls_boxes = pred_boxes[inds, :]
            else:
              cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4] #[300, 4]

            cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1) # [300, 5]
            # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
            cls_dets = cls_dets[order]
            # keep = nms(cls_dets, cfg.TEST.NMS)
            keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS) #[N, 1]
            cls_dets = cls_dets[keep.view(-1).long()]#[N, 5]

            all_boxes[j][i] = cls_dets.cpu().numpy()
          else:
            all_boxes[j][i] = empty_array

      # Limit to max_per_image detections *over all classes*
      if max_per_image > 0:
          image_scores = np.hstack([all_boxes[j][i][:, -1]
                                    for j in xrange(1, imdb.num_classes)])#[M,]
          if len(image_scores) > max_per_image:
              image_thresh = np.sort(image_scores)[-max_per_image]
              for j in xrange(1, imdb.num_classes):
                  keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                  all_boxes[j][i] = all_boxes[j][i][keep, :]

      detect_time1 = _t1['im_detect'].toc(average=True)
      print('current_im_detect: {:d}/{:d} {:.3f}s'.format(i + 1, num_images, detect_time1))

      misc_toc = time.time()
      nms_time = misc_toc - misc_tic

      sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
          .format(i + 1, num_images, detect_time, nms_time))
      sys.stdout.flush()



  with open(det_file, 'wb') as f:
      pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

  print('Evaluating detections')
  imdb.evaluate_detections(all_boxes, output_dir)

  end = time.time()
  print("test time: %0.4fs" % (end - start))


def test_several_weights():
    weight_dir=r"models/res101/weight_htcn_ga s_gas_composite-gas_real_7"################################################
    weight_list=os.listdir(weight_dir)
    weight_list.sort(key=lambda x:int(x[x.index("step")+5:x.index(".pth")]))# sort by step
    weight_list.sort(key=lambda x: int(x[x.index("epoch") + 6:x.index("_step")]))# sort by epoch
    for weight in weight_list:
        print("-"*200)
        print(weight)
        weight_path = os.path.join(weight_dir,weight)
        test_one_weight(weight_path)


if __name__ == '__main__':
  weight_path=r"weight/weight_htcn_res101_gas_composite18.1-real7_epoch2_step6999_no-interpolation.pth"
  test_one_weight(weight_path)   # weight set in parser_func.py

  # test_several_weights()






  
