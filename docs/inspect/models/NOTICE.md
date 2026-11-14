`deployed.onnx` and `baseline.onnx` are this project's belt-damage detectors (`models/belt_damage_yolo11n.pt` and
`models/belt_damage_yolo11n_baseline.pt`), exported with `make inspect-web` (`vision/export_web.py`).

They were fine-tuned from Ultralytics YOLO11n (COCO-pretrained). Ultralytics distributes YOLO11 under the AGPL-3.0
licence (https://github.com/ultralytics/ultralytics/blob/main/LICENSE). The training images come from the dataset
credited in `../samples/NOTICE.md` (MIT licence).
