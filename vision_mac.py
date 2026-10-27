"""Runs inside small worker processes on a Mac: asks Apple's built-in Vision framework
(on-device, uses the Neural Engine) what is in a photo.

Needs:  pip install pyobjc-framework-Vision   (the app can install it for you)
"""
from __future__ import annotations

import os

# Labels that suggest a photo has readable text worth extracting.
TEXT_HINT = {"document", "screenshot", "text", "paper", "receipt", "sign", "signage", "poster", "menu",
             "book", "handwriting", "whiteboard", "printed_page", "chart", "map", "ticket", "letter"}
MIN_CONF = 0.30
MAX_TAGS = 12


def check():
    """(ok, reason) — can this Python talk to Apple's Vision framework?"""
    try:
        import objc  # noqa: F401
        import Foundation  # noqa: F401
        import Vision  # noqa: F401
        Vision.VNClassifyImageRequest  # present on macOS 10.15+
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def init():
    """Worker start-up: run politely in the background."""
    try:
        os.nice(10)
    except Exception:
        pass
    import objc  # noqa: F401
    import Vision  # noqa: F401


def _perform(path, requests):
    import Foundation
    import Vision
    url = Foundation.NSURL.fileURLWithPath_(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    res = handler.performRequests_error_(requests, None)
    ok, err = (res if isinstance(res, tuple) else (res, None))
    if not ok:
        raise RuntimeError(str(err) if err is not None else "Vision could not read this image")


def analyze(job):
    """job = (media_id, thumbnail_path, original_path, ext, kind) → dict of results."""
    mid, thumb, orig, ext, kind = job
    out = {"id": mid, "tags": "", "faces": 0, "vtext": None}
    try:
        import objc
        import Vision
        with objc.autorelease_pool():
            classify = Vision.VNClassifyImageRequest.alloc().init()
            faces = Vision.VNDetectFaceRectanglesRequest.alloc().init()
            animals = Vision.VNRecognizeAnimalsRequest.alloc().init()
            _perform(thumb, [classify, faces, animals])

            labels = []
            for o in (classify.results() or []):
                c = float(o.confidence())
                if c >= MIN_CONF:
                    labels.append((str(o.identifier()).lower(), c))
            labels.sort(key=lambda x: -x[1])
            tags = [name for name, _ in labels[:MAX_TAGS]]
            for o in (animals.results() or []):
                for lab in list(o.labels() or [])[:1]:
                    t = str(lab.identifier()).lower()
                    if t not in tags:
                        tags.append(t)
            out["faces"] = len(faces.results() or [])
            out["tags"] = " ".join(tags)

            # Read text only where it's likely to matter (screenshots, documents, signs) — keeps it light.
            name = os.path.basename(orig).lower()
            if ext == ".png" or "screenshot" in name or "screen shot" in name or any(t in TEXT_HINT for t in tags):
                src = thumb
                try:
                    if kind == "photo" and os.path.exists(orig) and os.path.getsize(orig) < (40 << 20):
                        src = orig  # full resolution reads much better than a thumbnail
                except OSError:
                    pass
                tr = Vision.VNRecognizeTextRequest.alloc().init()
                tr.setRecognitionLevel_(getattr(Vision, "VNRequestTextRecognitionLevelFast", 1))
                tr.setUsesLanguageCorrection_(False)
                _perform(src, [tr])
                lines = []
                for o in (tr.results() or []):
                    cands = o.topCandidates_(1)
                    if cands is not None and len(cands):
                        lines.append(str(cands[0].string()))
                if lines:
                    out["vtext"] = " ".join(lines)[:2000]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
