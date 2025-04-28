# PhilEO-MajorTOM: Scaling-up the pretraining of Geospatial Foundation Models

# Table Of Contents
1. [Introduction](#introduction)
2. [Datasets](#data)
3. [New Models](#newmodels)


## Introduction <a name="introduction"></a>
This repository is an extension of the previously introduced [PhilEO Bench](https://arxiv.org/abs/2401.04464), and is linked to [paper]. The [PhilEO Bench] serves as a framework that allows users to benchmark various
Geospatial Foundation Models (GFMs) against each other on three downstream tasks: land cover classification, building density estimation and road density estimation. In [paper], we extend on the PhilEO Bench, by upscaling the 
pretraining of the Geo-Aware U-Net to subsets from [MajorTOM](https://github.com/ESA-PhiLab/Major-TOM). Moreover, we show that the PhilEO ViT UPerNet outperforms its C-NN decoder-based counterparts on all three downstream tasks. 


## Datasets <a name="data"></a>
The datasets used for pretraining are extracted from the [MajorTOM](https://github.com/ESA-PhiLab/Major-TOM) repo. In particular, we pretrained the Geo-Aware U-Net on the MajorTOM 23TB Sentinel-2 dataset, and its smaller 2TB subset, called FastTOM.
This yields increased performance w.r.t. the previously used 0.5TB PhilEO Globe dataset. For finetuning, we use the labelled 0.4TB PhilEO Bench [downstream](https://huggingface.co/datasets/PhilEO-community/PhilEO-downstream) dataset.






