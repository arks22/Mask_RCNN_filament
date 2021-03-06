"""
佐々木実験5のスクリプト

過学習を防ぐため、datasetに2013,2014,2015の画像を追加
validationは2016 #-> load_maskでエラーが出るのでいくつかのファイルを削除
epochを半分に削減

学習用
$ python3 filament.py train

検証用
$ python3 filament.py evaluate --model=last --eval_type=xxxx --year=xxxxx

predict画像出力はinspect*.pyを実行
"""

import os
import sys
import time
import numpy as np
import imgaug
import json
import collections as cl

ROOT_DIR = os.path.abspath("../")
CURRENT_DIR = os.getcwd()
DEFAULT_LOGS_DIR = os.path.join(CURRENT_DIR, "logs")
DEFAULT_DATASET_DIR = os.path.join(CURRENT_DIR, "dataset")

sys.path.append(ROOT_DIR)

from mrcnn.config import Config
from mrcnn import model as modellib, utils

from pycocotools.coco import COCO
from pycocotools import mask as maskUtils
from pycocotools.cocoeval import COCOeval

os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
import tensorflow as tf
import keras



class FilamentConfig(Config):
    # Give the configuration a recognizable name
    NAME = "Filament"

    # We use a GPU with 12GB memory, which can fit two images.
    # Adjust down if you use a smaller GPU.
    IMAGES_PER_GPU = 1

    # Number of classes (including background)
    NUM_CLASSES = 1 + 1  # ARorQRの2通り＋Background

    RPN_ANCHOR_SCALES = (128, 256, 512)
    RPN_ANCHOR_RATIOS = [0.5, 1, 2,]

    BACKBONE = "resnet50"

    STEPS_PER_EPOCH = 1#デバッグ用
    VALIDATION_STEPS = 1#デバッグ用


    #IMAGE_MAX_DIM = 768



class FilamentDataset(utils.Dataset):
    def load_coco(self, dataset_dir, subset=None,return_coco=False, year=2016):
        if subset=='train':
            coco = COCO("{}/annotations/datasets_train.json".format(dataset_dir))
            image_dir = "{}/train_jpg".format(dataset_dir)

        else:
            coco = COCO("{}/annotations/datasets_val_{}.json".format(dataset_dir, year))
            image_dir = "{}/val_jpg_{}".format(dataset_dir, year)

        class_ids = sorted(coco.getCatIds())
        # print("class:{}".format(class_ids))
        image_ids = list(coco.imgs.keys())
        # print("image:{}".format(image_ids))

        #self.add_class("filament", 1, "filament")
        
        for i in class_ids:
            self.add_class("coco", i, coco.loadCats(i)[0]["name"])
        
        for i in image_ids:
            #print(i, "\n")
            self.add_image(
                "coco", image_id=i,
                path=os.path.join(image_dir, coco.imgs[i]['file_name']),
                width=coco.imgs[i]["width"],
                height=coco.imgs[i]["height"],
                annotations=coco.loadAnns(coco.getAnnIds(
                    imgIds=[i], catIds=class_ids, iscrowd=None)))
        #print(self.image_info)
        #sys.exit()
        if return_coco:
            return coco


    def load_mask(self, image_id):
        # Build mask of shape [height, width, instance_count] and list of class IDs that correspond to each channel of the mask.
        #print(self.image_info)
        image_info = self.image_info[image_id]
        instance_masks = []
        class_ids = []
        annotations = self.image_info[image_id]["annotations"]

        #print("image id" + image_info["id"])
        
        for annotation in annotations:
            class_id = self.map_source_class_id("coco.{}".format(annotation['category_id']))
            #print(annotation["id"])
            if class_id:
                m = self.annToMask(annotation, image_info["height"],image_info["width"])
                # Some objects are so small that they're less than 1 pixel area
                # and end up rounded out. Skip those objects.
                if m.max() < 1:
                    continue
                # Is it a crowd? If so, use a negative class ID.
                instance_masks.append(m)
                class_ids.append(class_id)
        
        if class_ids:
            mask = np.stack(instance_masks, axis=2).astype(np.bool)
            class_ids = np.array(class_ids, dtype=np.int32)
            return mask, class_ids
        else:
            return super(FilamentDataset, self).load_mask(image_id)


    def annToRLE(self, ann, height, width):
        segm = ann['segmentation']
        if isinstance(segm, list):
            # polygon -- a single object might consist of multiple parts
            # we merge all parts into one mask rle code
            rles = maskUtils.frPyObjects(segm, height, width)
            rle = maskUtils.merge(rles)
        elif isinstance(segm['counts'], list):
            # uncompressed RLE
            rle = maskUtils.frPyObjects(segm, height, width)
        else:
            # rle
            rle = ann['segmentation']
        return rle
        
    def annToMask(self, ann, height, width):
        """
        Convert annotation which can be polygons, uncompressed RLE, or RLE to binary mask.
        :return: binary mask (numpy 2D array)
        """
        rle = self.annToRLE(ann, height, width)
        m = maskUtils.decode(rle)
        return m

def build_coco_results(dataset, image_ids, rois, class_ids, scores, masks):
    """Arrange resutls to match COCO specs in http://cocodataset.org/#format
    """
    # If no results, return an empty list
    if rois is None:
        return []

    results = []
    for image_id in image_ids:
        # Loop through detections
        for i in range(rois.shape[0]):
            class_id = class_ids[i]
            score = scores[i]
            bbox = np.around(rois[i], 1)
            mask = masks[:, :, i]

            result = {
                "image_id": image_id,
                "category_id": dataset.get_source_class_id(class_id, "coco"),
                "bbox": [bbox[1], bbox[0], bbox[3] - bbox[1], bbox[2] - bbox[0]],
                "score": score,
                "segmentation": maskUtils.encode(np.asfortranarray(mask))
            }
            results.append(result)
    return results
    
def evaluate_coco(model, dataset, coco, eval_type=None, limit=0, image_ids=None):
    # Pick COCO images from the dataset
    image_ids = image_ids or dataset.image_ids

    # Limit to a subset
    if limit:
        image_ids = image_ids[:limit]

    # Get corresponding COCO image IDs.
    coco_image_ids = [dataset.image_info[id]["id"] for id in image_ids]

    t_prediction = 0
    t_start = time.time()

    results = []
    for i, image_id in enumerate(image_ids):
        # Load image
        image = dataset.load_image(image_id)
        # print(image.shape)
        # Run detection
        t = time.time()
        r = model.detect([image], verbose=0)[0]
        t_prediction += (time.time() - t)

        # Convert results to COCO format
        # Cast masks to uint8 because COCO tools errors out on bool
        image_results = build_coco_results(dataset, coco_image_ids[i:i + 1],
                                            r["rois"], r["class_ids"],
                                            r["scores"],
                                            r["masks"].astype(np.uint8))
        results.extend(image_results)

    # Load results. This modifies results with additional attributes.
    coco_results = coco.loadRes(results)

    # Evaluate
    cocoEval = COCOeval(coco, coco_results, eval_type)
    cocoEval.params.imgIds = coco_image_ids
    cocoEval.evaluate()
    cocoEval.accumulate()
    cocoEval.summarize()

    print("Prediction time: {}. Average {}/image".format(
        t_prediction, t_prediction / len(image_ids)))
    print("Total time: ", time.time() - t_start)

class GetLosses(keras.callbacks.Callback):
    def __init__(self):
        self.train_loss           = [0] * 1000
        self.rpn_class_loss       = [0] * 1000
        self.rpn_bbox_loss        = [0] * 1000
        self.mrcnn_class_loss     = [0] * 1000
        self.mrcnn_bbox_loss      = [0] * 1000
        self.mrcnn_mask_loss      = [0] * 1000
        self.val_loss             = [0] * 1000
        self.val_rpn_class_loss   = [0] * 1000
        self.val_rpn_bbox_loss    = [0] * 1000
        self.val_mrcnn_class_loss = [0] * 1000
        self.val_mrcnn_bbox_loss  = [0] * 1000
        self.val_mrcnn_mask_loss  = [0] * 1000

    def on_epoch_end(self, epoch, logs={}):
        n = epoch
        self.train_loss[n]           = logs['loss']
        self.rpn_bbox_loss[n]        = logs['rpn_bbox_loss']
        self.rpn_class_loss[n]       = logs['rpn_class_loss']
        self.mrcnn_class_loss[n]     = logs['mrcnn_class_loss']
        self.mrcnn_bbox_loss[n]      = logs['mrcnn_bbox_loss']
        self.mrcnn_mask_loss[n]      = logs['mrcnn_mask_loss']
        self.val_loss[n]             = logs['val_loss']
        self.val_rpn_bbox_loss[n]    = logs['val_rpn_bbox_loss']
        self.val_rpn_class_loss[n]   = logs['val_rpn_class_loss']
        self.val_mrcnn_class_loss[n] = logs['val_mrcnn_class_loss']
        self.val_mrcnn_bbox_loss[n]  = logs['val_mrcnn_bbox_loss']
        self.val_mrcnn_mask_loss[n]  = logs['val_mrcnn_mask_loss']

def dump_loss(get_losses, best_epoch, timestamp):
    #Dump result to json
    js = cl.OrderedDict()

    js["train_loss"]       = get_losses.train_loss[0:best_epoch]
    js["rpn_class_loss"]   = get_losses.rpn_class_loss[0:best_epoch]
    js["rpn_bbox_loss"]    = get_losses.rpn_bbox_loss[0:best_epoch]
    js["mrcnn_class_loss"] = get_losses.mrcnn_class_loss[0:best_epoch]
    js["mrcnn_bbox_loss"]  = get_losses.mrcnn_bbox_loss[0:best_epoch]
    js["mrcnn_mask_loss"]  = get_losses.mrcnn_mask_loss[0:best_epoch]
    js["val_loss"]             = get_losses.val_loss[0:best_epoch]
    js["val_rpn_class_loss"]   = get_losses.val_rpn_class_loss[0:best_epoch]
    js["val_rpn_bbox_loss"]    = get_losses.val_rpn_bbox_loss[0:best_epoch]
    js["val_mrcnn_class_loss"] = get_losses.val_mrcnn_class_loss[0:best_epoch]
    js["val_mrcnn_bbox_loss"]  = get_losses.val_mrcnn_bbox_loss[0:best_epoch]
    js["val_mrcnn_mask_loss"]  = get_losses.val_mrcnn_mask_loss[0:best_epoch]

    jsonfilename = 'loss_log/loss_' + timestamp + '.json'
    fw = open(jsonfilename,'w')
    json.dump(js,fw)
    print("Saved losses to : " + jsonfilename) 


if __name__ == '__main__':
    import argparse
        # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Train Mask R-CNN on MS COCO.')
    parser.add_argument("command",
                        metavar="<command>",
                        help="'train' or 'evaluate' on MS COCO")
    parser.add_argument('--dataset', required=False,
                        default=DEFAULT_DATASET_DIR,
                        metavar="/path/to/coco/",
                        help='Directory of the MS-COCO dataset')
    parser.add_argument('--model', required=False,
                        default="imagenet",
                        metavar="/path/to/weights.h5",
                        help="Path to weights .h5 file or 'coco'")
    parser.add_argument('--logs', required=False,
                        default=DEFAULT_LOGS_DIR,
                        metavar="/path/to/logs/",
                        help='Logs and checkpoints directory (default=logs/)')
    parser.add_argument('--limit', required=False,
                        default=500,
                        metavar="<image count>",
                        help='Images to use for evaluation (default=500)')
    parser.add_argument('--year', required=False,
                        default=2016,
                        metavar="<image count>",
                        help='Images to use for evaluation (default=500)')
    parser.add_argument('--eval_type', required=False,
                        metavar="<evaluate type>",
                        help='Evaluate Annotation type')
    args = parser.parse_args()

    print("Command:       ", args.command)
    print("Model:         ", args.model)
    print("Dataset:       ", args.dataset)
    print("Logs:          ", args.logs)
    print("Limit:         ", args.limit)
    print("Validation:    ", args.year)
    print("Evaluate Type: ", args.eval_type)

        # Configurations
    if args.command == "train":
        config = FilamentConfig()
    else:
        class InferenceConfig(FilamentConfig):
            # Set batch size to 1 since we'll be running inference on
            # one image at a time. Batch size = GPU_COUNT * IMAGES_PER_GPU
            GPU_COUNT = 1
            IMAGES_PER_GPU = 1
            DETECTION_MIN_CONFIDENCE = 0
        config = InferenceConfig()

    config.display()

     # Create model
    if args.command == "train":
        model = modellib.MaskRCNN(mode="training", config=config, model_dir=args.logs)
    else:
        model = modellib.MaskRCNN(mode="inference", config=config, model_dir=args.logs)
    
    # Select weights file to load
    if args.model.lower() == "last":
        # Find last trained weights
        model_path = model.find_last()
    elif args.model.lower() == "imagenet":
        # Start from ImageNet trained weights
        model_path = model.get_imagenet_weights()
    else:
        model_path = args.model

    # Load weights 
    print("Loading weights ", model_path)
    model.load_weights(model_path, by_name=True)
    print("model load completed")

    if args.command == "train":
        # Training dataset. Use the training set and 35K from the
        # validation set, as as in the Mask RCNN paper.
        dataset_train = FilamentDataset()
        dataset_train.load_coco(args.dataset,"train")
        dataset_train.prepare()

        # Validation dataset
        dataset_val = FilamentDataset()
        dataset_val.load_coco(args.dataset,"val",year=args.year)
        dataset_val.prepare()

        #GetLosses
        get_losses = GetLosses()

        # Image Augmentation
        # Right/Left flip 50% of the time
        augmentation = imgaug.augmenters.Fliplr(0.5)

                #Training - Stage 1
        print("Training network heads")
        model.train(dataset_train, dataset_val,
                    learning_rate=config.LEARNING_RATE,
                    epochs=20,
                    layers='heads',
                    custom_callbacks=[get_losses],
                    augmentation=augmentation)
                # Training - Stage 2
        #Finetune layers from ResNet stage 4 and up
        print("Fine tune Resnet stage 4 and up")
        model.train(dataset_train, dataset_val,
                    learning_rate=config.LEARNING_RATE,
                    epochs=60,
                    layers='4+',
                    custom_callbacks=[get_losses],
                    augmentation=augmentation)

        # Training - Stage 3
        # Fine tune all layers
        print("Fine tune all layers")
        model.train(dataset_train, dataset_val,
                    learning_rate=config.LEARNING_RATE / 10,
                    epochs=80,
                    layers='all',
                    custom_callbacks=[get_losses],
                    augmentation=augmentation)

        timestamp = os.path.basename(model.log_dir)
        dump_loss(get_losses, 80, timestamp)

    elif args.command == "evaluate":
        # Validation dataset
        dataset_val = FilamentDataset()
        coco = dataset_val.load_coco(args.dataset,"val",return_coco=True, year=args.year)
        dataset_val.prepare()
        print("Running COCO evaluation on {} images.".format(args.limit))
        if not args.eval_type:
            print("Error: Please specify the evaluation type.")
            exit(1)
        evaluate_coco(model, dataset_val, coco, args.eval_type, limit=int(args.limit))
