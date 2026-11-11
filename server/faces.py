"""Local YuNet/SFace inference and persistent, user-labelled face enrollment."""
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone

import numpy as np
from pillow_heif import register_heif_opener

register_heif_opener(thumbnails=False)

MATCH_THRESHOLD = .50
MATCH_MARGIN = .08
CLUSTER_THRESHOLD = .65
MODEL_HASHES = {
    "face_detection_yunet_2023mar.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    "face_recognition_sface_2021dec.onnx": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def normalized(vector):
    result = np.asarray(vector, dtype=np.float32).reshape(-1)
    if result.size != 128 or not np.isfinite(result).all() or np.linalg.norm(result) < 1e-8:
        raise ValueError("Invalid SFace embedding")
    return result / np.linalg.norm(result)


def validate_name(name):
    name = " ".join(name.strip().split())
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Enter a name between 1 and 80 characters.")
    return name


def prepare_photo(data):
    """Validate before decoding; orient phone photos and strip original metadata."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ("JPEG", "MPO", "PNG", "WEBP", "HEIF", "AVIF"):
                raise ValueError(f"The file contains {image.format or 'an unknown format'}. Use a JPEG, PNG, WebP, HEIC/HEIF, or AVIF photo.")
            if image.width * image.height > 24_000_000 or max(image.size) > 12000:
                raise ValueError("Photo is too large. Use an image up to 24 megapixels.")
            if getattr(image, "is_animated", False) and image.format not in ("MPO", "HEIF"):
                raise ValueError("Use a still photo rather than an animated image.")
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            result = BytesIO()
            image.save(result, format="JPEG", quality=92)
            return result.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("This file could not be read as a photo. Use JPEG, PNG, WebP, HEIC/HEIF, or AVIF.") from exc


class OpenCVEngine:
    """Use only from one inference worker; OpenCV model objects are stateful."""
    def __init__(self, directory):
        import cv2
        self.cv = cv2
        cv2.setNumThreads(2)
        paths = {}
        for name, digest in MODEL_HASHES.items():
            path = Path(directory) / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Face models missing or invalid. Run scripts/download-face-models.py, then restart the server.")
            paths[name] = str(path)
        self.detector = cv2.FaceDetectorYN.create(paths["face_detection_yunet_2023mar.onnx"], "", (320, 320), .9, .3, 100)
        self.recognizer = cv2.FaceRecognizerSF.create(paths["face_recognition_sface_2021dec.onnx"], "")

    def detect(self, jpeg):
        cv = self.cv
        if len(jpeg) > 4 * 1024 * 1024:
            raise ValueError("Camera image is too large")
        image = cv.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv.IMREAD_COLOR)
        if image is None:
            raise ValueError("Camera did not return a readable JPEG")
        height, width = image.shape[:2]
        if width > 960:
            image = cv.resize(image, (960, round(height * 960 / width)))
        height, width = image.shape[:2]
        candidates = []
        # YuNet needs faces at suitable pixel sizes: a full-frame close-up may
        # disappear at native resolution. Detect at several scales, then align
        # and assess the face on the original image.
        for edge in sorted(set((min(320, max(width, height)), min(640, max(width, height)), max(width, height)))):
            scale = edge / max(width, height)
            scan = image if scale == 1 else cv.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))))
            self.detector.setInputSize((scan.shape[1], scan.shape[0]))
            _, faces = self.detector.detect(scan)
            for face in ([] if faces is None else faces):
                face = face.copy()
                # Coordinates and five landmarks scale; confidence does not.
                face[:14] /= scale
                candidates.append(face)
        indices = cv.dnn.NMSBoxes([face[:4].tolist() for face in candidates], [float(face[-1]) for face in candidates], .9, .3) if candidates else []
        result, rejected, reasons = [], 0, []
        for index in np.asarray(indices).reshape(-1)[:12]:
            face = candidates[int(index)]
            x, y, w, h = [float(v) for v in face[:4]]
            x1, y1 = max(0, int(x)), max(0, int(y))
            x2, y2 = min(width, int(x + w)), min(height, int(y + h))
            crop = image[y1:y2, x1:x2]
            if crop.size == 0 or min(x2 - x1, y2 - y1) < 48:
                rejected += 1
                reasons.append("The face is too small. Crop closer to the face or use a higher-resolution photo.")
                continue
            # Comparing native-resolution Laplacians wrongly rejects enlarged
            # clear portraits. Measure on a fixed-sized face crop instead.
            gray = cv.resize(cv.cvtColor(crop, cv.COLOR_BGR2GRAY), (112, 112))
            blur = float(cv.Laplacian(gray, cv.CV_64F).var())
            if blur < 35:
                rejected += 1
                reasons.append("The detected face looks blurry. Use a sharper photo with the eyes clearly visible.")
                continue
            aligned = self.recognizer.alignCrop(image, face)
            embedding = normalized(self.recognizer.feature(aligned))
            thumb = cv.resize(crop, (160, max(1, round(crop.shape[0] * 160 / crop.shape[1]))))
            ok, encoded = cv.imencode(".jpg", thumb, [cv.IMWRITE_JPEG_QUALITY, 88])
            if not ok:
                continue
            result.append({"embedding": embedding, "thumbnail": encoded.tobytes(),
                           "quality": float(face[-1]) * min(w, h),
                           "box": [x1, y1, x2 - x1, y2 - y1]})
        return {"faces": result, "rejected": rejected, "rejected_reasons": reasons}


class FaceStore:
    def __init__(self, data_dir):
        self.db = sqlite3.connect(Path(data_dir) / "faces.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL, thumbnail BLOB NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sightings (
                id TEXT PRIMARY KEY, camera_url TEXT NOT NULL,
                person_id TEXT REFERENCES people(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL, thumbnail BLOB NOT NULL, quality REAL NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, observations INTEGER NOT NULL,
                similarity REAL);
        """)

    def match(self, embedding):
        vector = normalized(embedding)
        scores = {}
        for row in self.db.execute("SELECT person_id,embedding FROM samples"):
            score = float(vector @ np.frombuffer(row["embedding"], dtype=np.float32))
            scores[row["person_id"]] = max(score, scores.get(row["person_id"], -1))
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        if not ranked:
            return None, None
        person_id, score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1
        if score < MATCH_THRESHOLD or score - runner_up < MATCH_MARGIN:
            return None, score
        return person_id, score

    def observe(self, detection, camera_url):
        vector = normalized(detection["embedding"])
        person_id, score = self.match(vector)
        current = utcnow()
        row = None
        if person_id:
            row = self.db.execute("SELECT * FROM sightings WHERE camera_url=? AND person_id=? ORDER BY last_seen DESC LIMIT 1", (camera_url, person_id)).fetchone()
        else:
            best_score = CLUSTER_THRESHOLD
            for candidate in self.db.execute("SELECT * FROM sightings WHERE camera_url=? AND person_id IS NULL", (camera_url,)):
                similarity = float(vector @ np.frombuffer(candidate["embedding"], dtype=np.float32))
                if similarity >= best_score:
                    row, best_score = candidate, similarity
        capture_id = row["id"] if row else uuid.uuid4().hex
        if row:
            better = detection["quality"] > row["quality"]
            self.db.execute("UPDATE sightings SET last_seen=?,observations=observations+1,similarity=?,embedding=?,thumbnail=?,quality=? WHERE id=?", (
                current, score, vector.tobytes() if better else row["embedding"],
                detection["thumbnail"] if better else row["thumbnail"],
                detection["quality"] if better else row["quality"], capture_id))
        else:
            self.db.execute("INSERT INTO sightings VALUES (?,?,?,?,?,?,?,?,?,?)", (
                capture_id, camera_url, person_id, vector.tobytes(), detection["thumbnail"], detection["quality"], current, current, 1, score))
        # Thumbnails and embeddings have bounded retention.
        self.db.execute("DELETE FROM sightings WHERE person_id IS NULL AND id NOT IN (SELECT id FROM sightings WHERE person_id IS NULL ORDER BY last_seen DESC LIMIT 100)")
        self.db.execute("DELETE FROM sightings WHERE person_id IS NOT NULL AND id NOT IN (SELECT id FROM sightings WHERE person_id IS NOT NULL ORDER BY last_seen DESC LIMIT 300)")
        self.db.commit()
        return capture_id

    def enroll(self, capture_id, name):
        name = validate_name(name)
        capture = self.db.execute("SELECT * FROM sightings WHERE id=?", (capture_id,)).fetchone()
        if capture is None:
            raise KeyError("Face capture no longer exists. Refresh and try again.")
        person = self.db.execute("SELECT * FROM people WHERE name_key=?", (name.casefold(),)).fetchone()
        if capture["person_id"]:
            if person and capture["person_id"] == person["id"]:
                return dict(person)
            raise ValueError("This capture is already enrolled. Remove the incorrect profile first if needed.")
        if person is None and self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0] >= 100:
            raise ValueError("The local gallery supports up to 100 people. Remove an unused profile first.")
        person_id = person["id"] if person else uuid.uuid4().hex
        with self.db:
            if person is None:
                self.db.execute("INSERT INTO people VALUES (?,?,?,?)", (person_id, name, name.casefold(), utcnow()))
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?)", (uuid.uuid4().hex, person_id, capture["embedding"], capture["thumbnail"], utcnow()))
            self.db.execute("DELETE FROM samples WHERE person_id=? AND id NOT IN (SELECT id FROM samples WHERE person_id=? ORDER BY created_at DESC LIMIT 20)", (person_id, person_id))
            self.db.execute("UPDATE sightings SET person_id=?,similarity=NULL WHERE id=?", (person_id, capture_id))
        return dict(self.db.execute("SELECT id,name,created_at FROM people WHERE id=?", (person_id,)).fetchone())

    def enroll_photos(self, name, detections):
        name = validate_name(name)
        if not detections:
            raise ValueError("No usable face photos were provided.")
        samples = [(normalized(d["embedding"]).tobytes(), d["thumbnail"]) for d in detections]
        person = self.db.execute("SELECT * FROM people WHERE name_key=?", (name.casefold(),)).fetchone()
        if person is None and self.db.execute("SELECT COUNT(*) FROM people").fetchone()[0] >= 100:
            raise ValueError("The local gallery supports up to 100 people. Remove an unused profile first.")
        person_id = person["id"] if person else uuid.uuid4().hex
        with self.db:
            if person is None:
                self.db.execute("INSERT INTO people VALUES (?,?,?,?)", (person_id, name, name.casefold(), utcnow()))
            for embedding, thumbnail in samples:
                self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?)", (uuid.uuid4().hex, person_id, embedding, thumbnail, utcnow()))
            self.db.execute("DELETE FROM samples WHERE person_id=? AND id NOT IN (SELECT id FROM samples WHERE person_id=? ORDER BY created_at DESC LIMIT 20)", (person_id, person_id))
        # Uploaded enrollment images are not camera sightings.
        return dict(self.db.execute("SELECT id,name,created_at FROM people WHERE id=?", (person_id,)).fetchone())

    def gallery(self, camera_url):
        people = [dict(row) for row in self.db.execute("SELECT p.id,p.name,p.created_at,COUNT(s.id) AS samples,MAX(s.created_at) AS updated_at FROM people p JOIN samples s ON s.person_id=p.id GROUP BY p.id ORDER BY p.name COLLATE NOCASE")]
        for person in people:
            person["image_url"] = f"/api/people/{person['id']}/image?v={person['updated_at']}"
        captures = [dict(row) for row in self.db.execute("SELECT s.id,s.person_id,p.name,s.first_seen,s.last_seen,s.observations,s.similarity,s.quality FROM sightings s LEFT JOIN people p ON p.id=s.person_id WHERE s.camera_url=? ORDER BY s.last_seen DESC", (camera_url,))]
        for capture in captures:
            capture["image_url"] = f"/api/faces/{capture['id']}/image?v={capture['quality']}"
        return {"people": people, "unknowns": [c for c in captures if c["person_id"] is None],
                "recent": [c for c in captures if c["person_id"] is not None][:12]}

    def image(self, identifier, person=False):
        if person:
            row = self.db.execute("SELECT thumbnail FROM samples WHERE person_id=? ORDER BY created_at DESC LIMIT 1", (identifier,)).fetchone()
        else:
            row = self.db.execute("SELECT thumbnail FROM sightings WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("Image not found")
        return row[0]

    def delete_person(self, person_id):
        with self.db:
            result = self.db.execute("DELETE FROM people WHERE id=?", (person_id,))
        if not result.rowcount:
            raise KeyError("Person not found")

    def close(self):
        self.db.close()
