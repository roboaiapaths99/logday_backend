try:
    import cv2  # type: ignore[import-not-found,import-untyped]
    HAS_CV2 = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    HAS_CV2 = False

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]
import base64

try:
    from deepface import DeepFace  # type: ignore[import-not-found]
    HAS_DEEPFACE = True
except ImportError:
    DeepFace = None  # type: ignore[assignment]
    HAS_DEEPFACE = False
import tempfile
import os
import logging

logger = logging.getLogger(__name__)



def decode_image(base64_string):
    """Decodes a base64 string into an OpenCV image."""
    if cv2 is None:
        logger.warning("OpenCV (cv2) module is not installed.")
        return None
    try:
        # Remove header if present
        if "," in base64_string:
            base64_string = base64_string.split(",")[1]
        
        encoded_data = base64.b64decode(base64_string)
        nparr = np.frombuffer(encoded_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        logger.error(f"Error decoding image: {e}")
        return None



# Test bypass for headless persona verification (Commented for Production)
DUMMY_IMAGE_BYPASS = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="

def get_face_embedding(img_base64):
    """Generates a face embedding from a base64 image string using in-memory NumPy arrays and strict face detection."""
    env = os.getenv("APP_ENV", "development").lower()
    # In production, dummy bypass is strictly disabled
    if env != "production" and (not HAS_DEEPFACE or img_base64 == DUMMY_IMAGE_BYPASS):
        return [0.1] * 4096
    if env == "production" and not HAS_DEEPFACE:
        logger.error("DeepFace is required in production environment.")
        return None

    try:
        img = decode_image(img_base64)
        if img is None:
            return None

        # Generate embedding directly from the in-memory BGR NumPy array (zero disk temp-file I/O).
        # Always enforce_detection=True across all fallbacks so background/no-face images never produce false vectors.
        results = None
        for backend in ("retinaface", "mtcnn", "opencv"):
            try:
                results = DeepFace.represent(
                    img_path=img,
                    model_name="VGG-Face",
                    detector_backend=backend,
                    enforce_detection=True,
                    align=True,
                )
                if results:
                    break
            except Exception as det_err:
                logger.warning(f"{backend} face detection failed: {det_err}")

        if results and len(results) > 0:
            return results[0]["embedding"]
        return None
    except Exception as e:
        logger.error(f"Critical error in face embedding: {e}")
        return None


def is_dummy_embedding(embedding):
    """Check if embedding is a dummy/mock constant vector (e.g., [0.1]*4096 or zero variance)."""
    if embedding is None:
        return True
    try:
        arr = np.array(embedding, dtype=np.float32)
        if arr.size == 0:
            return True
        if np.std(arr) < 1e-5:
            return True
        return False
    except Exception:
        return True


def verify_face(img_base64, stored_embedding, threshold=0.60):
    """Verifies a face against a stored embedding using cosine similarity."""
    env = os.getenv("APP_ENV", "development").lower()
    new_embedding = get_face_embedding(img_base64)
    if new_embedding is None:
        logger.warning("No face detected in the provided image.")
        return False, 1.1 # No face detected
    
    # If DeepFace is not installed, allow 1:1 test bypass only for local testing in development
    if env != "production" and not HAS_DEEPFACE and is_dummy_embedding(stored_embedding):
        logger.info("DeepFace unavailable: allowing 1:1 test bypass for enrolled user in development.")
        return True, 0.0

    a = np.array(new_embedding)
    b = np.array(stored_embedding)
    
    # Check for shape mismatch (e.g., 4096 vs 128)
    if a.shape != b.shape:
        logger.error(f"Face embedding shape mismatch: {a.shape} vs {b.shape}. User must re-enroll.")
        return False, 1.2 # Shape mismatch

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    
    if norm_a == 0 or norm_b == 0:
        logger.warning("Zero norm detected in face embedding comparison.")
        return False, 1.3 # Zero norm
        
    cos_sim = np.dot(a, b) / (norm_a * norm_b)
    distance = 1 - cos_sim
    
    return distance <= threshold, distance

def compare_faces(embedding1, embedding2, threshold=0.60):
    """Compares two embeddings and returns True if they match. Dummy embeddings never match in 1:N search."""
    if is_dummy_embedding(embedding1) or is_dummy_embedding(embedding2):
        return False
        
    a = np.array(embedding1)
    b = np.array(embedding2)
    
    if a.shape != b.shape:
        return False
        
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0: 
        return False

    cos_sim = np.dot(a, b) / (norm_a * norm_b)
    distance = 1 - cos_sim
    
    return distance <= threshold

