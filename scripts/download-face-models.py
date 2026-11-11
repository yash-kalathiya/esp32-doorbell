"""Download the official OpenCV Zoo models and verify their Git LFS SHA-256."""
import hashlib
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODELS = {
    "face_detection_yunet_2023mar.onnx": ("face_detection_yunet", "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": ("face_recognition_sface", "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}


def main():
    destination = ROOT / "models"
    destination.mkdir(exist_ok=True)
    for name, (folder, digest) in MODELS.items():
        path = destination / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            print(f"Verified {name}", flush=True)
            continue
        temporary = path.with_suffix(".tmp")
        url = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/{folder}/{name}"
        print(f"Downloading {name} from OpenCV Zoo…", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Checksum mismatch for {name}; refusing to install")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Verified {name}", flush=True)
    for name, folder in (("YuNet-LICENSE.txt", "face_detection_yunet"), ("SFace-LICENSE.txt", "face_recognition_sface")):
        url = f"https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/{folder}/LICENSE"
        with urllib.request.urlopen(url, timeout=30) as response:
            (destination / name).write_bytes(response.read())


if __name__ == "__main__":
    main()
