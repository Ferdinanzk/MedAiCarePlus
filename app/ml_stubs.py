"""Lightweight stubs for heavy ML packages (torch, cv2, mediapipe, etc.)

These are injected into sys.modules when the real packages aren't installed,
allowing the backend to start for local frontend testing without downloading
multi-gigabyte ML dependencies.
"""

import sys
import types
import warnings

# torch
class _FakeTensor:
    def __init__(self, *a, **k): pass
    def to(self, *a, **k): return self
    def eval(self): return self
    def unsqueeze(self, *a): return self
    def squeeze(self, *a): return self
    def cpu(self): return self
    def numpy(self):
        import numpy as np
        return np.array([])
    @classmethod
    def no_grad(cls):
        import contextlib
        return contextlib.nullcontext()

class _FakeDevice:
    def __init__(self, name): self.type = name

class _FakeCuda:
    is_available = staticmethod(lambda: False)

class _FakeNnModule:
    def __init__(self, *a, **k): pass
    def to(self, *a, **k): return self
    def eval(self): return self
    def load_state_dict(self, *a, **k): pass
    def __call__(self, x): return x

class _FakeSequential(list):
    def __init__(self, *items): super().__init__(items)
    def __call__(self, x):
        for m in self: x = m(x)
        return x

class _FakeConv2d(_FakeNnModule): pass
class _FakeReLU(_FakeNnModule): pass
class _FakeMaxPool2d(_FakeNnModule): pass
class _FakeFlatten(_FakeNnModule): pass
class _FakeLinear(_FakeNnModule): pass
class _FakeDropout(_FakeNnModule): pass

class _FakeNn(types.ModuleType):
    Module = _FakeNnModule
    Sequential = _FakeSequential
    Conv2d = _FakeConv2d
    ReLU = _FakeReLU
    MaxPool2d = _FakeMaxPool2d
    Flatten = _FakeFlatten
    Linear = _FakeLinear
    Dropout = _FakeDropout

class _FakeTorch(types.ModuleType):
    def __init__(self, name="torch"):
        super().__init__(name)
        self.nn = _FakeNn("torch.nn")
        self.cuda = _FakeCuda()
        self.device = _FakeDevice
        self.Tensor = _FakeTensor
    def no_grad(self):
        import contextlib
        return contextlib.nullcontext()
    @staticmethod
    def load(*a, **k): return {}
    @staticmethod
    def softmax(*a, **k):
        import numpy as np
        return np.array([])

# torchvision
class _FakeTform:
    def __init__(self, *a, **k): pass
    def __call__(self, img): return img

class _FakeCompose:
    def __init__(self, transforms): self.transforms = transforms
    def __call__(self, img):
        for t in self.transforms: img = t(img)
        return img

class _FakeTransforms(types.ModuleType):
    Compose = _FakeCompose
    Resize = _FakeTform
    ToTensor = _FakeTform
    Normalize = _FakeTform

class _FakeTorchvision(types.ModuleType):
    transforms = _FakeTransforms("torchvision.transforms")

# cv2
class _FakeCLAHE:
    def apply(self, *a, **k): return a[0]

class _FakeCv2(types.ModuleType):
    IMREAD_COLOR = 1
    COLOR_BGR2RGB = 4
    COLOR_BGR2LAB = 44
    COLOR_LAB2BGR = 56
    INTER_LINEAR = 1
    INTER_CUBIC = 2
    BORDER_REPLICATE = 1
    @staticmethod
    def imdecode(*a, **k): return None
    @staticmethod
    def cvtColor(*a, **k): return a[0]
    @staticmethod
    def resize(*a, **k): return a[0]
    @staticmethod
    def getRotationMatrix2D(*a, **k): return None
    @staticmethod
    def warpAffine(*a, **k): return a[0]
    @staticmethod
    def getPerspectiveTransform(*a, **k): return None
    @staticmethod
    def warpPerspective(*a, **k): return a[0]
    @staticmethod
    def arcLength(*a, **k): return 0
    @staticmethod
    def approxPolyDP(*a, **k): return None
    @staticmethod
    def minAreaRect(*a, **k): return None
    @staticmethod
    def boxPoints(*a, **k): return None
    @staticmethod
    def split(*a, **k): return a[0]
    @staticmethod
    def merge(*a, **k): return a[0]
    @staticmethod
    def createCLAHE(*a, **k): return _FakeCLAHE()
    @staticmethod
    def filter2D(*a, **k): return a[0]
    @staticmethod
    def fastNlMeansDenoisingColored(*a, **k): return a[0]
    @staticmethod
    def imencode(*a, **k): return True, b""

# mediapipe
class _FakeLandmark:
    x = 0.5
    y = 0.5
    z = 0

class _FakeMultiFace:
    landmark = [_FakeLandmark() for _ in range(468)]

class _FakeFaceMeshResult:
    multi_face_landmarks = [_FakeMultiFace()]

class _FakeFaceMesh:
    def __init__(self, *a, **k): pass
    def process(self, *a, **k): return _FakeFaceMeshResult()

class _FakeMpSolutions:
    class face_mesh:
        FaceMesh = _FakeFaceMesh

class _FakeMediapipe(types.ModuleType):
    solutions = _FakeMpSolutions()

# ultralytics
class _FakeYOLO:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return []

class _FakeUltralytics(types.ModuleType):
    YOLO = _FakeYOLO

class _FakeOps:
    @staticmethod
    def scale_image(*a, **k): return a[0]

class _FakeUltralyticsUtils(types.ModuleType):
    ops = _FakeOps()

# openvino
class _FakeOpenVino(types.ModuleType):
    pass


def install():
    stubs = {
        "torch": _FakeTorch(),
        "torch.nn": _FakeNn("torch.nn"),
        "torchvision": _FakeTorchvision("torchvision"),
        "torchvision.transforms": _FakeTransforms("torchvision.transforms"),
        "cv2": _FakeCv2("cv2"),
        "mediapipe": _FakeMediapipe("mediapipe"),
        "ultralytics": _FakeUltralytics("ultralytics"),
        "ultralytics.utils.ops": _FakeOps(),
        "openvino": _FakeOpenVino("openvino"),
    }
    for name, stub in stubs.items():
        if name in sys.modules:
            continue  # Already imported (real package)
        try:
            __import__(name)  # Try loading the real package
        except ImportError:
            sys.modules[name] = stub
            warnings.warn(f"[ml_stubs] Using stub for missing package: {name}", stacklevel=2)


if __name__ == "__main__":
    install()
