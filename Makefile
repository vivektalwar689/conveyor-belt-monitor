PY ?= .venv/bin/python
export OMP_NUM_THREADS ?= 2

.PHONY: setup setup-vision demo demo-novision test train eval vision-data vision-audit vision-train vision-eval inspect-web screenshots

setup:                ## core deps (simulator, backend, models, dashboard)
	python3 -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q -r requirements.txt

setup-vision:         ## + detector inference/training (torch via ultralytics)
	$(PY) -m pip install -q -r requirements-vision.txt

demo:                 ## whole system, dashboard on http://localhost:8000
	$(PY) -m scripts.demo

demo-novision:
	$(PY) -m scripts.demo --no-vision

test:
	$(PY) -m pytest -q

train:                ## simulate five 120-day runs, train + evaluate sensor models (~6 min)
	OMP_NUM_THREADS=1 $(PY) -m ml.train

vision-data:          ## fetch the public dataset (pinned commit) and audit/re-split it
	$(PY) -m vision.fetch_dataset && $(PY) -m vision.audit

vision-train:         ## YOLO11n on CPU (~3 h); the trained weights are committed in models/
	$(PY) -m vision.train_detector --split clean

vision-eval:          ## belt-loop evaluation with ground truth: deployed detector, then the baseline at a 0.25 floor (~10 min)
	$(PY) -m vision.loop_eval --passes 30 --out vision/artifacts/loop_eval_hardneg.json
	$(PY) -m vision.loop_eval --passes 30 --weights models/belt_damage_yolo11n_baseline.pt --min-conf 0.25 \
		--no-plot --out vision/artifacts/loop_eval_clean.json

inspect-web:          ## rebuild docs/inspect (ONNX models + samples) and check the page's maths against Ultralytics
	$(PY) -m vision.export_web
