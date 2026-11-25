"""Check the real YOLOX model, motion gate and event creation on a sample video.

Usage: python tests/check_person_video.py path/to/video.avi
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.environ.get("EAC_AI_APP_DIR", ROOT / "eac_vision_ai"))
sys.path.insert(0, str(APP_DIR))
import core  # noqa: E402
from ai import Detector, LABELS, VisionAI  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        parser.error("Video non leggibile")
    model = APP_DIR / "ai" / "yolox_tiny.onnx"
    with tempfile.TemporaryDirectory() as directory:
        core.DATA = Path(directory)
        cid = "sample-person"
        vision = SimpleNamespace(config={"cameras": {cid: {"name": "Video di prova"}}}, session=None, token=None)
        ai = VisionAI(vision)
        ai.detector = Detector(model)
        ai.runtime[cid] = {"motion": False, "counts": dict.fromkeys(LABELS, 0), "tracks": {},
                           "error": "", "watching": True, "last_infer": 0, "motion_until": 0,
                           "last_queued": 0, "frames": 0, "inferences": 0, "last_frame": 0}
        background = None
        frames = gated = people = 0
        for index in range(args.seconds * 5):
            cap.set(cv2.CAP_PROP_POS_MSEC, index * 200)
            ok, source = cap.read()
            if not ok:
                break
            height, width = source.shape[:2]
            ratio = min(640 / width, 360 / height)
            scaled = cv2.resize(source, (int(width * ratio), int(height * ratio)))
            frame = np.zeros((360, 640, 3), np.uint8)
            top, left = (360 - scaled.shape[0]) // 2, (640 - scaled.shape[1]) // 2
            frame[top:top + scaled.shape[0], left:left + scaled.shape[1]] = scaled
            frames += 1
            now = index * 200
            gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
            if background is None:
                background = gray.astype(np.float32)
                continue
            mask = cv2.absdiff(gray, cv2.convertScaleAbs(background)) > 25
            cv2.accumulateWeighted(gray, background, .08)
            fraction = np.count_nonzero(mask) / mask.size
            if .005 < fraction < .6:
                ai.runtime[cid]["motion_until"] = now + 3000
            if now >= ai.runtime[cid]["motion_until"]:
                continue
            gated += 1
            detections = ai.detector.infer(frame)
            if any(item["label"] == "persona" for item in detections):
                people += 1
            ai.follow(cid, frame, mask, now, detections)
        print(f"frames={frames} motion_frames={gated} person_frames={people} "
              f"person_events={sum(event['label'] == 'persona' for event in ai.recent)}")
        result = 0 if any(event["label"] == "persona" for event in ai.recent) else 1
    cap.release()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
