"""
MEA Visual Stimuli v2.1.3

Source export of the application code contained in the companion Jupyter
notebook. The notebook and this file are intended to remain functionally
equivalent.

See README.md, CHANGELOG.md and docs/MEA_Visual_Stimuli_Manual_v2_0.pdf
for documentation and validation scope.
"""

import json
import mmap
import os
import subprocess
import sys
import tempfile
import time
import math
import random
import atexit
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser, simpledialog, filedialog

APP_VERSION = "2.1.3"
MANUAL_FILENAME = "MEA_Visual_Stimuli_Manual_v2_0.pdf"

TRIGGER_SOURCE_EMBEDDED = 'class MaxOneFTDI:\n    """Shared FTDI backend for the operator diagnostic and display worker.\n\n    Logical signals use the validated inverted VisExpMan polarity.\n    USB/driver latency is measured by the experiment, not assumed zero.\n    """\n\n    FRAME_PULSE_MIN_S = 0.001\n\n    def __init__(self, port="auto"):\n        self.requested_port = str(port or "auto").strip() or "auto"\n        self.resolved_port = ""\n        self.serial = None\n        self._frame_high_at = None\n\n    def open(self):\n        if self.serial is not None:\n            raise RuntimeError("El puerto FTDI ya está abierto.")\n        try:\n            import serial\n        except ImportError as exc:\n            raise RuntimeError(\n                "MaxOne Legacy FTDI requiere PySerial. Instálalo en el "\n                "entorno de este notebook: %pip install pyserial"\n            ) from exc\n\n        port = self.requested_port\n        if port.lower() in ("auto", "automatico", "automático"):\n            from serial.tools import list_ports\n            matches = [\n                p for p in list_ports.comports()\n                if getattr(p, "vid", None) == 0x0403\n                and getattr(p, "pid", None) == 0x6001\n            ]\n            if not matches:\n                raise RuntimeError("No se encuentra el FTDI MaxOne (0403:6001).")\n            if len(matches) != 1:\n                devices = ", ".join(str(p.device) for p in matches)\n                raise RuntimeError(\n                    f"Hay varios FTDI 0403:6001 ({devices}). "\n                    "Indica el puerto del montaje en el campo Puerto."\n                )\n            port = str(matches[0].device)\n\n        try:\n            # Configure the requested rest state before opening where the\n            # driver permits it; reapply both outputs immediately afterwards.\n            self.serial = serial.Serial(\n                port=None, baudrate=9600, timeout=0, write_timeout=0,\n                rtscts=False, dsrdtr=False, xonxoff=False,\n            )\n            self.serial.rts = True\n            self.serial.break_condition = True\n            self.serial.port = port\n            self.serial.open()\n            self.resolved_port = port\n            self.idle(strict=True)\n        except Exception as exc:\n            self.close()\n            raise RuntimeError(\n                f"No se pudo abrir/configurar {port}: {type(exc).__name__}: {exc}"\n            ) from exc\n        return self.resolved_port\n\n    def _set(self, signal, logical):\n        if self.serial is None:\n            raise RuntimeError("El backend MaxOne FTDI no está abierto.")\n        physical = not bool(logical)\n        if signal == "frame":\n            self.serial.rts = physical\n        else:\n            self.serial.break_condition = physical\n\n    def set_frame(self, logical):\n        self._set("frame", logical)\n        self._frame_high_at = _ftdi_time.perf_counter() if logical else None\n\n    def set_event(self, logical):\n        self._set("event", logical)\n\n    def frame_high(self):\n        self.set_frame(1)\n\n    def frame_low(self):\n        # Keep the requested RTS HIGH for at least 1 ms after the driver call.\n        # A bounded short wait avoids scheduling a Timer across later frames.\n        # Physical width can be longer; validate it in /bits on the real setup.\n        if self._frame_high_at is not None:\n            deadline = self._frame_high_at + self.FRAME_PULSE_MIN_S\n            while _ftdi_time.perf_counter() < deadline:\n                pass\n        self.set_frame(0)\n\n    def idle(self, strict=False):\n        errors = []\n        if self.serial is not None:\n            # Attempt BOTH lines even if one write fails.\n            for name, action in (("FRAME", self.set_frame), ("EVENT", self.set_event)):\n                try:\n                    action(0)\n                except Exception as exc:\n                    errors.append(f"{name}: {type(exc).__name__}: {exc}")\n        self._frame_high_at = None\n        if errors and strict:\n            raise RuntimeError("; ".join(errors))\n        return errors\n\n    def close(self):\n        errors = self.idle()\n        ser, self.serial = self.serial, None\n        if ser is not None:\n            try:\n                ser.close()\n            except Exception as exc:\n                errors.append(f"Cerrar FTDI: {type(exc).__name__}: {exc}")\n        return errors\n\n\nclass GenericTTLFTDI:\n    """Shared FTDI backend for the generic trigger box and display worker.\n\n    Validated hardware mapping (active-low FTDI control-line polarity):\n      UNIVERSAL / FRAME_SYNC -> RTS#  (ser.rts = False)\n      AUX EVENT              -> TX/BREAK (break_condition = False)\n\n    Encoding on UNIVERSAL is generated in software:\n      visual flip -> FRAME pulse on RTS\n      condition onset -> RTS EVENT pulse ~3 ms later + simultaneous BREAK EVENT\n    The EVENT replication onto RTS uses _set() directly so it never overwrites\n    the visual FRAME anchor used for timing and logging.\n    """\n\n    FRAME_PULSE_MIN_S = 0.001\n    EVENT_PULSE_MIN_S = 0.001\n    EVENT_OFFSET_S = 0.003\n\n    def __init__(self, port="auto"):\n        self.requested_port = str(port or "auto").strip() or "auto"\n        self.resolved_port = ""\n        self.serial = None\n        self._frame_high_at = None\n        self._last_frame_anchor = None\n        self._event_high_at = None\n\n    @staticmethod\n    def _wait_until(deadline):\n        """Hybrid sleep/spin wait, matching the validated UNIVERSAL tester."""\n        while True:\n            remaining = float(deadline) - _ftdi_time.perf_counter()\n            if remaining <= 0:\n                return\n            if remaining > 0.002:\n                _ftdi_time.sleep(max(0.0, remaining - 0.001))\n            # Final sub-millisecond section intentionally busy-waits.\n\n    def open(self):\n        if self.serial is not None:\n            raise RuntimeError("El puerto FTDI ya está abierto.")\n        try:\n            import serial\n        except ImportError as exc:\n            raise RuntimeError(\n                "Trigger Box TTL genérica requiere PySerial. Instálalo en el "\n                "entorno de este notebook: %pip install pyserial"\n            ) from exc\n\n        port = self.requested_port\n        if port.lower() in ("auto", "automatico", "automático"):\n            from serial.tools import list_ports\n            matches = [\n                p for p in list_ports.comports()\n                if getattr(p, "vid", None) == 0x0403\n                and getattr(p, "pid", None) == 0x6001\n            ]\n            if not matches:\n                raise RuntimeError("No se encuentra el FTDI de la Trigger Box (0403:6001).")\n            if len(matches) != 1:\n                devices = ", ".join(str(p.device) for p in matches)\n                raise RuntimeError(\n                    f"Hay varios FTDI 0403:6001 ({devices}). "\n                    "Indica manualmente el puerto de la Trigger Box."\n                )\n            port = str(matches[0].device)\n\n        try:\n            self.serial = serial.Serial(\n                port=None, baudrate=9600, timeout=0, write_timeout=0,\n                rtscts=False, dsrdtr=False, xonxoff=False,\n            )\n            # Validated idle polarity: both FTDI lines physically inactive.\n            self.serial.rts = True\n            self.serial.break_condition = True\n            self.serial.port = port\n            self.serial.open()\n            self.resolved_port = port\n            self.idle(strict=True)\n        except Exception as exc:\n            self.close()\n            raise RuntimeError(\n                f"No se pudo abrir/configurar {port}: {type(exc).__name__}: {exc}"\n            ) from exc\n        return self.resolved_port\n\n    def _set(self, signal, logical):\n        if self.serial is None:\n            raise RuntimeError("El backend FTDI genérico no está abierto.")\n        physical = not bool(logical)\n        if signal == "frame":\n            self.serial.rts = physical\n        elif signal == "event":\n            self.serial.break_condition = physical\n        else:\n            raise ValueError(f"Señal FTDI desconocida: {signal}")\n\n    def set_frame(self, logical):\n        self._set("frame", logical)\n        now = _ftdi_time.perf_counter()\n        if logical:\n            self._frame_high_at = now\n            self._last_frame_anchor = now\n        else:\n            self._frame_high_at = None\n        return now\n\n    def set_event(self, logical):\n        self._set("event", logical)\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = now if logical else None\n        return now\n\n    def frame_high(self):\n        return self.set_frame(1)\n\n    def frame_low(self):\n        # Keep FRAME HIGH for at least 1 ms. The validated hardware produced\n        # ~1.1-1.35 ms physical pulses with this requested width.\n        if self._frame_high_at is not None:\n            self._wait_until(self._frame_high_at + self.FRAME_PULSE_MIN_S)\n        return self.set_frame(0)\n\n    def _event_pair_high(self):\n        """Assert EVENT on AUX EVENT and replicate it onto UNIVERSAL."""\n        if self.serial is None:\n            raise RuntimeError("El backend FTDI genérico no está abierto.")\n        errors = []\n        # RTS carries UNIVERSAL. BREAK carries the independent AUX EVENT.\n        # Use _set() directly for RTS so the visual FRAME anchor is NOT changed.\n        for signal in ("frame", "event"):\n            try:\n                self._set(signal, 1)\n            except Exception as exc:\n                errors.append(f"{signal}: {type(exc).__name__}: {exc}")\n        if errors:\n            # Best-effort fail-safe: release both outputs before propagating.\n            for signal in ("event", "frame"):\n                try:\n                    self._set(signal, 0)\n                except Exception:\n                    pass\n            raise RuntimeError("; ".join(errors))\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = now\n        return now\n\n    def _event_pair_low(self):\n        """Release AUX EVENT and UNIVERSAL after an EVENT pulse."""\n        errors = []\n        # Release UNIVERSAL first, then AUX EVENT; both operations are attempted.\n        for signal in ("frame", "event"):\n            try:\n                self._set(signal, 0)\n            except Exception as exc:\n                errors.append(f"{signal}: {type(exc).__name__}: {exc}")\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = None\n        if errors:\n            raise RuntimeError("; ".join(errors))\n        return now\n\n    def pulse_event_now(self, width_s=None):\n        """Generate EVENT simultaneously on UNIVERSAL (RTS) and AUX EVENT (BREAK)."""\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        high_at = self._event_pair_high()\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            low_at = self._event_pair_low()\n        return {\n            "event_high_perf_s": high_at,\n            "event_low_perf_s": low_at,\n            "event_width_s": max(0.0, low_at - high_at),\n        }\n\n    def pulse_event_after_frame(self, offset_s=None, width_s=None):\n        """Emit EVENT as the second UNIVERSAL pulse after the last visual FRAME."""\n        if self._last_frame_anchor is None:\n            raise RuntimeError(\n                "No hay un FRAME_SYNC previo al que asociar el EVENT."\n            )\n        offset = self.EVENT_OFFSET_S if offset_s is None else max(0.0, float(offset_s))\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        frame_anchor = float(self._last_frame_anchor)\n        self._wait_until(frame_anchor + offset)\n\n        # IMPORTANT: this pulses RTS directly instead of set_frame(), so the\n        # original visual FRAME anchor remains unchanged for timing/logging.\n        high_at = self._event_pair_high()\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            low_at = self._event_pair_low()\n\n        return {\n            "frame_anchor_perf_s": frame_anchor,\n            "event_high_perf_s": high_at,\n            "event_low_perf_s": low_at,\n            "event_offset_s": max(0.0, high_at - frame_anchor),\n            "event_width_s": max(0.0, low_at - high_at),\n        }\n\n    def pulse_universal_event_after_frame(self, aux_state, offset_s=None, width_s=None):\n        """Pulse only UNIVERSAL ~3 ms after FRAME while setting AUX EVENT state.\n\n        This is the v1.27 experimental encoding used by the display worker:\n        RTS/UNIVERSAL keeps the validated short EVENT pulse, whereas TX/BREAK\n        becomes a held condition-state channel. The visual FRAME anchor is never\n        overwritten.\n        """\n        if self._last_frame_anchor is None:\n            raise RuntimeError(\n                "No hay un FRAME_SYNC previo al que asociar el EVENT."\n            )\n        offset = self.EVENT_OFFSET_S if offset_s is None else max(0.0, float(offset_s))\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        frame_anchor = float(self._last_frame_anchor)\n        desired_aux = 1 if int(aux_state) else 0\n        self._wait_until(frame_anchor + offset)\n\n        universal_high_at = None\n        aux_transition_at = None\n        try:\n            # UNIVERSAL receives the precise second pulse; use _set() directly so\n            # the FRAME anchor remains the original visual flip.\n            self._set("frame", 1)\n            universal_high_at = _ftdi_time.perf_counter()\n            # AUX EVENT is a held state, changed at the same condition onset.\n            self._set("event", desired_aux)\n            aux_transition_at = _ftdi_time.perf_counter()\n            self._event_high_at = aux_transition_at if desired_aux else None\n            self._wait_until(universal_high_at + width)\n        finally:\n            # Release ONLY UNIVERSAL. AUX EVENT deliberately remains at its\n            # requested condition state until the next onset or explicit reset.\n            self._set("frame", 0)\n            universal_low_at = _ftdi_time.perf_counter()\n\n        return {\n            "frame_anchor_perf_s": frame_anchor,\n            "event_high_perf_s": universal_high_at,\n            "event_low_perf_s": universal_low_at,\n            "event_offset_s": max(0.0, universal_high_at - frame_anchor),\n            "event_width_s": max(0.0, universal_low_at - universal_high_at),\n            "aux_transition_perf_s": aux_transition_at,\n            "aux_state": desired_aux,\n        }\n\n    def pulse_aux_marker(self, width_s=None):\n        """Emit a short marker only on AUX EVENT (TX/BREAK).\n\n        UNIVERSAL/RTS is deliberately untouched. This is used for protocol\n        pause markers in v1.28. AUX always returns LOW when the pulse ends.\n        """\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        # Start from a defined LOW state, then generate a clean HIGH pulse.\n        self._set("event", 0)\n        self._event_high_at = None\n        self._set("event", 1)\n        high_at = _ftdi_time.perf_counter()\n        self._event_high_at = high_at\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            self._set("event", 0)\n            low_at = _ftdi_time.perf_counter()\n            self._event_high_at = None\n        return {\n            "aux_high_perf_s": high_at,\n            "aux_low_perf_s": low_at,\n            "aux_width_s": max(0.0, low_at - high_at),\n        }\n\n    def idle(self, strict=False):\n        errors = []\n        if self.serial is not None:\n            # Attempt BOTH lines even if one write fails.\n            for name, action in (("FRAME", self.set_frame), ("EVENT", self.set_event)):\n                try:\n                    action(0)\n                except Exception as exc:\n                    errors.append(f"{name}: {type(exc).__name__}: {exc}")\n        self._frame_high_at = None\n        self._last_frame_anchor = None\n        self._event_high_at = None\n        if errors and strict:\n            raise RuntimeError("; ".join(errors))\n        return errors\n\n    def close(self):\n        errors = self.idle()\n        ser, self.serial = self.serial, None\n        if ser is not None:\n            try:\n                ser.close()\n            except Exception as exc:\n                errors.append(f"Cerrar FTDI: {type(exc).__name__}: {exc}")\n        return errors\n'
exec(TRIGGER_SOURCE_EMBEDDED, globals())

# SMN-Systems splash artwork embedded inside the notebook.
# It is a resized copy of the selected original image and requires
# no external image file at runtime.
SPLASH_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAhwAAAIcCAYAAAC9/nd8AAEAAElEQVR42qT9d9xmSVnnj7+rzrnzkzuH6enJASYwiZwEBCSJiChiFsN30Z9h1dVFAd3VNbGs"
    "Lob94qq4gIAKSBzCMMDknFN3T+fc/eTnjudU/f44qapOnftp9jv7wp3pfp47nFOn6ro+1yeIOFaa9B8hKP+jAQFCCLRO/kAg0Gh08gcI6xcFQmh09rvJn6DR"
    "VP1TvLbxS4BGI7QAkfw72n0v0Nr+3Frr0s+Yn93+s/Q7CJG+tvEaiIoPW/25s/eu/L66eI3s/YsX1en/J4qL7nsNIZIvrbMbpo17Z34n8u+QXDuR/4x1D9OL"
    "KJIPBVoXL+1c3+y6Fp+b/NpRvBpCVy2m9L3N7559n8rFkbye9yeytWl+T9xrS+laJt89/WOlk0tOds9Fei/ta5p91Ox3szucfPziuqVfPv9O5lq01hwifb38"
    "DoKw153W2loz/udG+5/V7JtoXXpGSpcxXS/FlRT5vQeNTD+odp6R7AIIofNbmKyj5LPk10NTuRbMZZBfS+eBKf5cW59XIKzrm13XbC266yJ7yeR37LXuXsv8"
    "WU6f8eQ9dXpNPWsDAbL43sLYH+x9Unuuf3oNJPnrl7eN4vvY10n7HgnPgtHe62/9t06vmxD25xK+s0AX54FM9oD8v7P1okivob1n5PcjuxfGHoK5tY3ZE6qO"
    "k3xPdi6kdS7oYpsVpf0jXRPWc2h/HiGy72vslcJ+5rVnTzN/r3wd7fPT3Bar1ma2xr3vZ7xAcp5JkMk9Se6RcRnTcyf5ntrak/KTTBtXRuDdl3T2uAvvdoVA"
    "ILMtNn8Vne/iCFG8oLn3aZT3xYwfT/9d5B903PrRVQdOviCz1xfOlpg8pOYfCM9KTV5fVy7S5MClusgwN1KKz5Jdhux38wfVLJqsva44lPPvrMkfdLNKKw5k"
    "96Oq4vNo8/UoHz7G/4RziBUbpXW3rLfM7pt5iJgLWgiBVqr4C23/jHtfs8/uXvvKYtR723Txf0WxHpM1qfNC2D6MtOcJ0HmhJbT15Pl3tvSHtFbJ/6z7qzFq"
    "ldI6y37GuqeCfHvT6etam65ROJY/u7APXkHp5/JyUqyzeeO5RDq9NunGl3x3zw0W5XoxX4/mmij9vPbuIcnrqPR/zjMk7Aeq9Kzq4vVFxTqy74u215Ou2B+U"
    "sxDzX3MaH2OhCmu92w9isUfYF17k912UPq8wm7esiEFlp4e1rkT+XXRxHbWymyhRUYcIxhcpwn6us0INs2gsHkrva+Q/r8uFjkZX1dbe+ynG/YDnTKnaZ6wd"
    "yF0n7vuUHwF7o9fV51mybyirQS1fI1U+N5T27qn5+aq1v6ZEGIVBdk10+e7qbK2q0svo8hddvw70Pm/JjhSSV89pJYddaZpvLZxK0Yc2FA+Qe/j6K7H8z3R5"
    "BWXFBsrYpNOfU2Z1KrA6DS2SmymR1gFvdUMqeR0ppf3AapFv1MWCE9aGooW2Ciml7YKk6LLcroPKBe/+kFs124tMFxW6Z+NNUBazCBWV567Zdal0QQpEqSNx"
    "/yzv6LN1I4V1D7XvflZcBfPPi/Xgbdsq+jdRKtAUKkHHnOfE/QxaKOPw0KVCVQgbVKq6f3kpLERp9y664aKuz+oDhV0sZIdJZfGriwIgO0R01eOvtYW2ZRtU"
    "jhhSRq7y7hSNTD9Dsr5NZEo7RWTV6i7+Lu/mnJ+Qwl6n2mgQzI7ULDTN/Uep4imT7nc1kQFhrzd3fZqIiLW3iYprhP382ciALh/KgnKhb3bzwrMGRfF+UggU"
    "VtfnICkmAiKs50YLs0OlAlH0I3JVqGKyPtK903n2reIH7TnAbUTBeqrzvUuvW3OY+4J1Dng23HzNpOeDVUML84WcJ09UP+9uEWbeZx/ApI3rQnrOZPdWj3vD"
    "0l5prG6hbZTN7VOF+YBV7xZjrrJ95lPqSp3nxGgQdPkzyOyIsS+Y9nZq+cPjOSis8YCyu3nSql0r7bZDpercXk3JxdSi6JvsMirZ8FTaHeqsW1Ykm6VTNGAU"
    "eb4CPNkoFFLYUK0i/QzZ/3TRPeYTGWsFq3QclHSJaF2N4giQQhaLxapslV1UuIWsrEBK3erWuffZAScQ6T2h1FWK/BrovEsy77l2v1OGXmiRjm/szqcK0XI3"
    "JKWUXWxWbXwVcKpEFh/ZRE90hoUoAzo10Ty30NGl62FfF+N9tQfyNNC44vfK46hsBGejd8XrmQdoXgyYEL2nSVKq6GjN+6QwkDTP8y2lLP4dCTo75HT5Wppn"
    "l6bUdeWFaPr/zC4suUuyXOIJvIedkMI76lQZuiaKfcZq5HS2ldlfVGllX1fj8HBRSu3r0LFgXOPQSdEvZ48z38NGFu2DcHwDaT/P1jrULtIsrAfPhN3LEH41"
    "IlnqoMFCm7PrKFQ6+taivA6M39dao7RK7qcs1rK2rrOyi2EHszcLOe2M37VW5UJFO81G9n7CvEfC2Vy19dxk+4KgfO/Nv6tCpa17htPQjvkF3+uZDUH2Hcyd"
    "xlq/wije0aBU5XuY60k4a02mYwQhBSLbIzQlGgLaQVGkcY6lRVpot3/Cnm9VzBp9EFVWrQlzlmPxCxg/qy+Db1aXVnQ37kIUCK2tDkJmeEw6KtFmkVKaRXvG"
    "FtZn1V60IOeUmAWt28E5CH3VwamdsjTjCJSv85jOXwjr7/P5otVF+VG3AoGiVNjYL6vt6yDMkUgx2zMfBBtt9PBwdAXaYaBNWTdqVfJiDNogilFegUQJHODK"
    "/zBrz70yuCK6NHISFqLnm8iUNnJpjBpFubiovMee4rMSHndGePmPinKRZ6EgorxxeouxKpTTeGEpZEWnqkvTjXHop58rlt0vbbbTpc83flBaXnd546U93DNf"
    "5+pBGoVwO1tdWlda++fxCLsgEEKWOCnlzUUXvJQSQrcOYiGqr4F20Oqx+7ZwBkU+DoPF2zHRGm0tUG3yqYSnoDXWrLUn5CijiajZXJ6CFyicMZ0wgcFSwebj"
    "Aq33/DGmpjCvsY93WHmtHW5Kgth4UGfnGTeBH1+RMZbikO7FaB/KU5DsNAW67uOxJcWpUrpyxIGfveMlqdj3DK3XLeDGr2APZGNdKKc4EOnm5kKe65BHfO23"
    "gQaIc5p16owDIkxIWNiwsO/aCWeULDxzOpsRW3FP7KLMvgbGQeJ8Z5EvFuHwTrQNVRowat6xuvCsB/42v6BJkSjIeEUBYo2VtG9eLMYWHPbaMItCbcHw7j02"
    "P1NRjXp4adpGJy0OhXNN3IPKvZ8+UrHW2kt+9Vbk6XsqrbyIo9uRngsyZBI413083WcMsyDTDuHSR9hMWDdVPIzvbq/QDpnEfqarCHf5v0tRENO1rixGKu+X"
    "KKPyVdeq6t8rOy/hmR8IX5HIOd87X8HiPVDXoXNokRIRtQZprEcctrlJpkw/v1LKHj/6ntGKEaswipISSV8XD2rxZ87BhI9zOmbhefhDiO92kbrXm0Ks4LyJ"
    "xqGDaaORz4uUKu6Edml2nuFzNjrHGpNoTztQGjFqY1z9XTyo5lqVbkurLOWEvzouNhRjti2wmLvWr2tdYgy7h6gwOjAL0hKl9WL/qtuJiDJigK54lHzVXV75"
    "yTKsJfzr0iWcZiNZ98eFEAmsZcLLwiZ0avd6GOhJ/gC7i8C87lZBpsf2eMlDm8DmFnyYwXQ6O4B1odDQOi+wzLFA1TOWsdDz18ugVXN0YoyILJWDeXAZFXne"
    "2ZiQrXPwK4OkV7WRCVOdpMm/q28EZhUbRreaVf6lDrSa8GGPJxwoPCdJV3SjJtRfIuCJ8fyYcdCt1r4xmQtjC+9YoTR+ENWdkvaoAEqvNYZ4nK9cU2EmPBjv"
    "mN/P17BxMCmlPJy08ljFNw4UZqHskrWFPSor76eiNDXJ4HJ77xHlPzN+0b+m7JrFvr/a+rP1ORMeYqcwrqF2yM7FJzMujSNJchoi+8HR5RGkudR11UZsqG1K"
    "F8IWLxSN9ZjRlNDl80Kvh/poz/GiK88e7Ta82sM613nXZyx34zkwijYpfCuOUkHjL76Scap3lGWKJoQoIZD+OYVdrIdu6SfGVDk+EozwAaWWlpJ8tiewSV9W"
    "EWKf+AhdbOHaQRH8KG1R6OSL0RIZlKtT/wU7F3ysuADaQjbSUYgx5jDln9rzGXzqTat71JS527qMGng3VavD8xPAhCg/CMLgywhEQdS3Nvzie/jGSCVhr9mZ"
    "OZt2jpw4kkhhtG9iHYjSWoPZPqEFWowvNE2ydH6VchKecasdgmE+mxfCOnDdA6GqkxX2/MzuWLUooz7rdMXu5/B35nosalEFA3uRE0/xVhSjohqlwS7O3IPf"
    "PUj8kK//aZUGimeijd5RDNUIWhVyNhbhoYzOmeNN93sXn0Wb01tr/blIrIns2aiN8nxHe2ohjOGwjRSOvz/2OM6xP9A2mR6HCpGRI825fmkn8tkc5M+9oYn3"
    "EEKFf0CHKXnGQanN8cO40a41Mhj7ecuoq9ZivAzd3e91ma+mS19H2OMePOMK7YDm3sOljDyb9hPe6ykY//xbZ9OYcbdOEQ5zo7D4GEKUxhneKax2SycX49P+"
    "ZSFlUZEaKEjG2s4ldOmBqY2yTBvPihJ2RVnJx1mv+6nohKsQjRxJyL+/QQvPyUmgS54f2ibZ6AoUZz0eWaXW24Ao9brT17K0C4d8pnVpT9XYRCGz0nZRj3Eb"
    "vvsFS12Qi2ToAlnI/1sL+xrqMoei1D+53ABhIx0lciDl6t5FI2wCdBkN8B3c7oadw6aeEVHV4W0hPlZ3RunvqjYLYRBYLQTA/K5jChXr4MZXWLvz9jIHzFSB"
    "6aqqwkANXFa+NqFql+Sry6ddFTfE7WLLI1pRljh7kAIL8XHWSXHd1pmm6PHYg0sCNcn77q5rAaRjDkShRbnRTPe6klrBVaAYzwXWZRc5adNFvIWDLlsHny4T"
    "azNPFIzxgo3pav++Oa4KEOX9dtyVL5G5LVQXbNlrxeUuH5He3shGh7SFdgunUa/agwv0J+XYufvSdzklcvcIG3DwFdnpPXQ5HPbIxGaX+zqt3MTIO3PEy5Ew"
    "V791gXKlTNFhmrOugvyXSbESd5kcbte2P0Py+ZXXScbLk6iYJWajEL3eorUIbEaF7ZK3tEYLH/nPZmsmm78C4UE4zFlwXlwaBZA5KxjjpGNzIxzZdjYt9bmA"
    "GfdReFkbrPt+/1f/aH8RoB2GVMbpKbpDE0Ub44Rl/rz7fdeb3bqFTmqKlM+3dfVmanZxJbMwz5jBB/9bEu8xs+j1eA3ZfZXYKJawunUK5MlFjJyueNz9Hocg"
    "VJpxmeo41+RLmwRK7e3Wq651JTdFiJKizvp5a3/ATzxmfQPEsfwB9/DPCo11DN1MkqoQZYQOsc5awYPWOi9oSp6F5/v6DBFNI0bfWq4qAnOjtmLCYHzOMnfK"
    "NMEznynloKbuZ60GtXUJ1as0wfJVlM7+WZgHFs+cNlFPrfJ9PZPpOyWa59zAbm5zxOvcicCuFQTCfx1MYrj1rJU4jynGptMr7/IH8MFNHl8FZZg5CavFFOO7"
    "8opF5o5QXMq3QFjnOu7IJR81iDGlrK780+rF/l0cOB7+iPBbd9o63dLPVPssZNcmmT37UCWBwQn1bKhO0VJJtEsPb629ZFQqWM6mP8A5kay0o4Ax56fCHeHZ"
    "68skzWnTnyWXL1evNapI0tqHtBQLz3vIVRCLS5uZ8LvZmuiGl1xoomOUvSEq1/A5kjG9ZGMpjXsjvHP1ca6I48YWJeRgvWp1XLepGV9wVYwKxqIc3ue76B4z"
    "3kdigKdLaLA4hz30/1PlbULxeAjNYHHtyg1GNVnW5WlVFST2WiseVPvzaMMoUHtRpnO5Bzb/wNbyWd+RavI8FUTJqsfEJAevVwwVa2xcEW6TWbUhIxTfRZPl"
    "rcQ8DYJvn/aa1I25/uY5n+95jisyHoNsX4FtIxzac8G0ORdfD3oRuf8E5yC3cQ2qvJWpWTkLcxaJRdxWRmdqdmDZJuTCuSbBiHM9FD1jC3eOl5MaSW0yUjSj"
    "6uabqiAtyrbgJQZ6ycvdvxi1O1owDxFrjljubIUD6VqL2CigxLmOe1yEigqSpfA8vEKc84ZdPmj1eIQNm+FNVbe7TtFZcv+DskeK9jyk4w4805qYspGpWGdN"
    "lsY46O9OBeLcZzwd5HfdsVc+S+spJAzkE41WGuHZ5YSUjqjAHmFWKcbcAs+V87ozbt8asSSf4tyQnHGIj68L/a4OZ4/0cZzdv18Z4zyDzrPqO4iKz6cdawJR"
    "IKeZbDXzhfChpt8VEurGEfgaPbcJsIF417itWB+GuZoHARn3DPjtBB1000VgfTYNZtFjodeu4qX8y1WfoeyZ4CBijm16qUH6vyychVJKn8tCtvTiWni6ONO/"
    "wvaKE1WIw7iu0jjgLJjIhfG17UBoLRpd1d1idQfaeA+z67Xlrn7b63U3Es8YQnv4DMXncq6V6b2V+zaYuR66EjkpoUTO+MX8+6JLzzpbUbjh6WJeWnVAF3dc"
    "VB5+vhm6cF6DtHgsULgC7XAfbq9Pwzl0ycIYdwlLIlsxPvFKfykbFInqDaiqMyo/e8LSlVt+AoZV4XqcinPH9jwL7Rx2k/WklGVV5LgADA+E+118epFtwoZx"
    "WWFWJgyTKFFWjTnz6PU6+dJYKWs0pCfHyCU66nMYs2jnNDQh9IpRkH8NpNioEOdUHI5bn9YeZTl1lqXgJbQD2yYhl2Cbf24aEZ6DV5PAI8M1zlvvCnZyYOyF"
    "aiPEJppHhffUOZsR6iIJpwwpaO8YxKUGWGeHKFmeGHw97VX/lPpV9xRySd8ebx3XlNA3UmEddKiEcJQeqnUWtaiAjSz1iRCViId3HugeOhmsi63fNnkQ2qNi"
    "sEYrpVl8cefKGmTseVkFd02s1w1TTfwqQcsu5OUWMdb4o0zpwIO0YCFC1RVv6cDL/juF0k0oWQuPOZB3jl9xGOmKB85XdLqf+Bwraj+cPWas5Q2RE6WH3e2W"
    "CrRClw4E37p2PWT0WJQn69bWn22b5FCfhNUuxoQNdhkjp2Kz93AqzGK1akFR9vIQyY7kkVMJ26FwPQTEaCByuXqlp0GVl5DJzLezdlxZ4Xc9bqkio7rws7VU"
    "jD/Dg1oKqkefDpfAbMyoGqlZj5IoRo1ajy3ryiqMKk6YNqY8hoIlRTqELmzFWcejqdLoze3WSyiqrRxct2wdM9738hzGNI/r+qs4YZuuQrOk3HH87HJOo++S"
    "jaMnaP1/tY/+3yCVVrwI2ssNC0ubl8bbRZYOSmMO7W0McghNFw9ExYXK/kZSJpyZnbpwD4CK0YBlcuXC5/khac4Ai/pf6KJLT7Iu1u8JvQeZqQQXpaPJmS0K"
    "x0CputHLupZxm6H2hkT5H1bvBms+UCXY2P78woG9tQML26RWY2SUu5IKy2fEPejsnADPSKoCMXJ7dqcKta+HLyfAK883szhNJ8viYBm3ETitVkq0d5j3+ev5"
    "U2ArDzhhd5BFn6aNtFN/SSwsVZqHZIcHdavCPUTVPmsHFNrkU+MVpElytOXhRVKjY5ImKiTj1tWssMv2rRsD8SsZmSlVvq/Czw8pd7R+hKhwKRbloi4/OMrj"
    "DNugU4yNT7Auj6jOVLFRat8I204OLtDMFIXMIgSE86QIH6LgOcQrLNuzZ0OPEyQgjKySMcVE9ufj+Ggaoxj3N6beosjZvwRlO3vtMd5yETtXRCwMTp97zmQN"
    "OBWf8btHO321ZJUZZRnt9SGV2e+ElRr7MXbkWpfZsVpQaXcnKubllreGU0yUCh7DAMo3mvDOso0KXlYeAJTGP6VZWgWhp0RMKslQjULN9JAp4c22tW+523c3"
    "Q8pESr0OrOV+cse9rgTNuZkP+UFUQTRzAAK/RXiVzbUYP2900LJiqfmRs3KhIfI0Tm/nZmn2tZWFoj3tmGlrL4TwfvBSGm++KYgSy1VTjB1cd0ATEhWZK2wR"
    "b5vkWViBijJHAXxgTYmnoRNOhNKjnMymVJx2/TEqTv8dZSTaWsBzOpMPkDJ5bxkE6XeRICRCBIZcUp5T7ZJL4pWyVBXKDWNzzV6Ec6haB6k9ivSufTypy8Z/"
    "CClRKs1IQlQWMe5rlRj8pZwgM7FaW8WwGWZncgp8I7CxAZmUM+K8BsHmWM+RXprE7Rws1oYNr5k6XjIprCacWTwPbwGVPgfCPoRLh7kHxdAe5YbwFCr2GMuU"
    "2wvWc7AdV+eVcnREVZSAKFWWttuoKI+2zf3L4QuaHlH5fq7LjVYVImob5DmNmhtWV3miFgUyOh2pVEmCKpy3q6FnB3pyob4qeMiaM2WjkqxQyP7OmbdbqIln"
    "YWvntaRxaHnhMfNGGZ9HmkmMYn0IGB9RU1RE6/nbi3KHXMVFcX5G+5ATfERgv6w2uwZqnJLAmevpEtTuMLqxMwuEcw+0rygtefB7Rgr6HDhC/zfVvG8UYprJ"
    "ub2yOJd5c/m6WJbcpW4G63DXmfSbJDxpHMSvtCIeDYmjASoeEQ8HxPGQaNhFRSO0GhKNBsTxCB0NiKIBOo7RKsKMMs8Cn5SKKotv27cnKBx6U6QkKzgQEiFD"
    "pAwRQYCQAUFQI6g1EbJGENYJam2Ceouw1kz/PiSo1ZEyHD8+SwMXzdGJD4rXlSNhs4e3cyF8sP24Wfc5eYxrZwTwXazXgsfj78bHzd6r5j9uBI3ZN/nn+SYZ"
    "3kjXdjgj/uLCRWfNPaNovuzzoII7sY7NuLcAWc+aPOdOFAZ8/jliNZettI8b+5ryoMqlz4aB+jojQ61Bygz1LxNdpZCFdbyH9/h/sf2V1rVv/Y/jc7lrKLR+"
    "sNqPqTI2yPbKd6AmrR3Ci52/Lnwwl68YkLLsOOpWZs6IQxhFh1n8+LroiugWu0MRtguiyV/QFeOMUmHkm5m4QUMOwVNXFTLlWYLnrwyHwlLnrzPvnMIZr8Jk"
    "SGRddbrBFFJobfd4udFUxdC1ovvyOs761DOOqZnPK8OXNaBZnzQnLOTOgIk9G7GVW4Nf0SlEeQRUQsMw3CK0ykfkKj24M6BCIK3vMhoOGPZXiQZdRsNVRr0V"
    "RoMu0bBLHHWJBz1UPEQayZuZG6XAJF6nqZ2GUZhpbAaCIEiMjosIcmF12japGRCx8dwXwwyldZJHnK6dTKFg85sCEGHiRyBDRFqUBLUWteYE9fY0Ya1DUGtS"
    "b01SqzVSnxNZPppVYZeUuJBKx73RREyzr2YT0PND1WeAV9EAVHWKpf3BGXGJCididIWSxGwwPHwDE6ETVkx9RZFkfB6t/fuJcJo8x/XCk4hZtjXPnxVzfil0"
    "iR/rK6IEYjySK0TFNR/fiJTs4p3rXE5QFFZT7K1chDvuFNWtpuuZUUE9yFA/4XBMiqbRdq11T3DhhG+uVyC4QgL7FYVNZNXCPz50ms+w2BSc1ELG+DZYLZUq"
    "QZKle5YVG9qfbWH9rucL5nPTKl99D0TtK5K8D5rLGajiNyjtjUvXzmhSeAoSk6mfVbqWeZH22/3mDqs4zn4GLCzGjRJ8I6ASI0GUFJylet7XcuX3EyfEthwK"
    "VTlQdg7fEtyfE1edg7/ivnoTSD2ze59E1LWP1yUjNuNBdWFZ68Lr8aO77J6aG5kQSBlY3ymKItSoS3f1LIPuMsPuEr3VeUaDLqP+KioaEBAlxvMSApkgB0KG"
    "BEFAGIYgQ2M8WMz8lZkyr4oN0cwEEqqojZUAoQryn211bB54GhVjkDTLzQAy4WLIwDYxEhmyoNPIeTUgHnUZDeYZRDEKlaqmJFpIwlozQUUaHZoTc9QaE9Rb"
    "UzQ7s4S1BkEQUmLrKF2WeWfHojB1LLosdV/H8VMIJ/OoCqEYpwLEdla2wpaszJ8Sc8ZfTGcvI+2iUBhnqR0AaCOiPh5YAeMbxQLCaqpMXYb25xsU19ksNiqO"
    "5Kqj2tpFhf+ApOLwrr5PwsvaKk5GaTQIokz2F256kSwsDwxXJe2eHxiGZsanUB5BhblXWdEZZoHjTgMcQrcWujxrPFf4w01wx8Nfqxo1KpXgM8XDoO2B/Lgb"
    "lKMH9nzIXKhWdLtBbsHtAH3doNEVC2esYhUwTvGhjWAw7YHyx5l8lc1iku5O+LoWbc+xCwKYXQ2XknTPCcsyDiZv/Hn5fvhItIxLyszVOkYwkBAwJlQ8H6NU"
    "auW1p1rB4sVS4jdUXAOtPVWz8BKvznV8UjD0q3+wcPQjh+i1PlcvVXMzNBG+QqIpkpS6/J9o2GfQX6W7fIrB2ll6K2cZdJeIh6tEwy46OcWRgUxGDWENKYMU"
    "Qk1OE6VtpCnjcUgpkFISSIGUgiAICEQi4xRGVIWUKR6iQKni82ql8ziMJHAveS9pPJcyRUGESF5HBiL57ww9EQVxVOkCBVNKEyuIYoVSilhlyIdOinLTntwg"
    "cmcbvo4jYhWhlELHCi0kMmwmCEh7hkZ7lubELM2JDdTrHcJ607nPqihyhDRGc7Z085yHclYIoKhee+ZmbBRvrq19adMekx/kbRKdZlTmaa3piFng5Q9YQYnO"
    "HN/cN92Rudmp4/Ix0p+1fInWKdBcYMEkcXpVIh4LBe2OXJ1sq8wYLWsqrUweF4gak2pbjLwcYz/pWRO6PCKyXGu97rpuQ1UABaY01U2RdkfvyX+nH0pUu1B7"
    "91eXY2JKenEMECtuaWizSMhdJcvkkHE+E85W7HIwfCMXs0s2ixOPgZev+xcW7On/bMpcr+bs37Ubd3wmbO5KAkkqrSvhUmVqo8eYJ5bUMw4By5K+irK+vDwO"
    "0iUijxM/ew6mZv7VITx1g0zXhnCQJPs9BH68A3ylTCnyHTee2d6qBYyFVC25r0/G6xbUFbPG4l1tzlExmnA7jHy6nZMsc7JoKusWQByP6K+eYdBdYHXxJP3l"
    "M/RWTxONuhCP0gIhIAhrhDKg1u7kt1EZgqZIaQKhCAMIwwTRCMK0mEg7oyjWjOKYXm9AfzBipTtiqRux3O2ztDKg1x+y1hux1h3SH45Y7Y7oD2P6w4hRpFBK"
    "MxhFxEqhVFJwKK1QeeBUcs0CmSZUBpJGLaAeBtRqAROtOq1GSLMRMNlu0GnWmGg3aLfrTLYbbJhq0m7Vk59r1anXQmrZ95ASrQVxrIgjRRQrYqWI4ux5EQhZ"
    "JwgahEIm11spIELFK/QX5umdVcRaIagT1BrU23PUOzO0p7bSaE3TnEjQkGwkk6FOWcEprN62rNLzzWKzMZXP6ns9kzRhyXeFSZUsEfXczBtrLfo2QqP5ER7U"
    "W/uePyfY0vLZcUctTrNjog3rqSVKKIpz+JqGedUEaLHuCEVXUQN0xaRag8w5fdoi9OZjZmsy5CERG2NaHwJifhZvoVTRiJbk6yVEt+w7Y+xQfuSnouDFLbgw"
    "CdzOWMpBm0qydq08YKFZ7Y7JGygUEH6Sn0sCLPE4nAvtzt8ztMKsUn1kHTecy0U2SnCac/FKN9gz8sE6sNwgIezIYjEmUdJjy50/pKpwGzWNhIR1cJZMffPN"
    "ILE8sLMdpBDjmQu6WDYihQC1JatMF5YUVmS8SyDLi9VKcpVvl9HFhu7cS7PESD6D8iIapvbe1zUKl/xZoWY3MxfMkDivHspAgpTWFkskUWEUrz0arNFdPs3a"
    "4knWFo/TXzvLoLeMigYIAaGUBGFIENYRMshRhUyNIaWkFkhqoUDK5Hpn1haDSLHai1hZG3BmYY0zSz1OnFnlxNk1Ti/3OL3QZ603YGG5x2AY0xtE9IcxkVKo"
    "uPD6kNmGKkxH34zLYRsCCFeei8nbSREMpa1Ro2/ZBYGkHib/azdrdFp1Jtt1pjp15mYm2DI3yY7N02ze0GJ2qsWW2QmmOg3arQbNeo1AJvcpjhWjKGY4jIii"
    "OOUXJVlLUiRIS8LpiIiiESqOUCpZ7bLeodaaoTOzjfb0VjpTm6i3JuykluwZ1AItnUA3Rb7pFg7DOn3PChUdYqzra9mDB69dupXjIag0ZMSHBjh8BJP/VBrB"
    "egjcPkKjy6HyHphVHBUz78TiCWmPukb4RwAO56OkVLGQjnL2CCUDQG01XtrZW0wSk8ArTvTMfB01YpX7qYPKu66xbn5Ldu+trBrzjDDG3lIYJYeLbon181Xy"
    "stMxx3RRh6p7IWIV63x6KzwOna7aQTg/W9FtyrSry/gX1mjF0yD4oBnrdQyimQXnuUVGeggWH912elsn1qlUKJi+qT5zF9MgzPaUsOlbyaJVdmiSYbdnxWln"
    "bn7mQyFMsyZDjpnDOLaXoo+h7EJewgnPq3q9qnwML2xnsNcrNz9hu9ZZUje3yjejy30KAZcAo8sFh7uRqhRKd+XMOWlQs07BYaeCmvyL/toSq4vHWVs4Sm/l"
    "NL3lM4yGq0itkUFIEIbIlF+QrUmlNVIIwiCgVgtp1EPCMERKwShSrPUGzC/3OXh8mSMnFzhyYokTZ3ocm1/l5EKPbnfAam/AYBSlnIyMdCoIJIQySMYxMuWI"
    "GjTLbOxR5Du45GZdECud+Tv+H8+nEkILhCw/XxlqpbQmVknRphXEcUysNSrWRCohn4aBpBYmCMlUp87cbIcdG6fZtW2aXVun2TQ3wXmbptg402ai06QRCiBm"
    "FMUMogwdiVFxgkwl45/kC8XRiHg0YBRHxFoS1No0JzfQmtrMxPR2WlObaLaninWejnsKYrrwH1JjOu0qRaCLmroeP+N+3v/3xXNso3EGZ8bsjPNAuKL9KIK+"
    "7C9j8ccyd9eUFCxcqTFlqarZIJrNoc8t2jYeKyS3pQIA26k0Pxo9456M9JxZOQgvd85uRlyLb/P183bNlECb+2m+UQgLebaiA6wxvUf96SPy+fbj3HzNzeEq"
    "RvR5gWw6rLroi8eRNbsvyi2ArDG5LYAoTQWUUrr0XXDRg3JFvJ5nwjiYz+cMqo0LPw5dsat/W68uPOxjMcanwcfULikgnBI0N0cTupp/kZOEXNpaRrCq6G4o"
    "jMecOZf9947dcdJs2RCa7xpY91J47le2UJCFfW1VqFDVfKMy2MlXcLjdlJ2FQInj4fUaHpMTWEFS9mVKaKwcHOGE6Wlj5zALjNGwz9rSSRZPHWTx9LP0lk6i"
    "Rj0CKag1mtRSmWeGFKnULTMQUAsFjXpIPZQoLegOYs4sDzhycpUDx+bZf3SRQydXOHZ6hVOLXRbXhoxGMQJNIARBmBQpYZAgWXkquEwKL9Mo1jRMKhAco4NO"
    "jcjM0LxSoSk8BGfKpjD5RicS3oA2ci7sa1+M6IQUOcHa/SdWyTgljnU6VlHEcaLqCQPJRLvJzGSLbVtmuHjHDOfvnOLSHRs5b9MUmza2mWg1CKUmimOGo5jh"
    "QBEpQ2ocpDiIiomiIUrFQIAMmzQ6M7Qmt9Ce205ncgv1ZtsgkseW9kJ48Hq3cTxXdQAu+bwqPbfCmmBsgJyLMlaoYqx9VWsvOT1vCEtRENmkUSeqK5+7r/Gz"
    "3i6+Ym+xDYpNZEk6plO6cEw2ZOiCgkti8cnS/zM26kN7fE/QJcTPW3CIde4P1cGX5bwfYbkVj0+5LU8DfGo+IcRYjyvTP6tKGeWODz0Fh04LPTWWEGN2n+dk"
    "5+rknpijiYLW7Q9PMxecdj3hPYmaeXKjQzJdL/VV44ceveWJEOvaaJjdi/VMZ2MSYY4XhOVtYFeZheNrIaOzpWU4CEtpYmeRLLV3gyp1VaLgHits2XKJ32CG"
    "VXqIrm6Cp0m69Lk4m2vLRB7EuBmwpkSa8t5rZwaaG2O5DnqlWbLKB0fZxqriiO7yWVYXj7F0ej/dpRMMuovoOKJWq1FrtAhqIUILojgxrpICamFAsxHSqAUo"
    "IVkbxJw6s8L+4ws8/expnj60yP7jy5w422NpbcBwFIEQhAHUw4AwlIRBYElbtU5GL1lFYSupDDzPVZWYHbbZEZpFP7bJm5lIbJfEumDjFw5OeXcsKbgnhfV6"
    "Mc4p6mezQwKroTXGFskIiKLo1xDFmmEcMxrFCfdEQz0ImWzX2byxw4XbZ7h81xwXn7+JC8+b47zN00x2QgIpGcWa4TBiOEq5KVIQBmFyTimNikZEUUSsNCJs"
    "UO/MMTGzk/bsNjpTGwnDWo5+5ORT4Z4w43NkPCYh1h5rOZ9WyG1L40Gty4VKjp6aAJX7bBfqM005lyWH6tN7bPkbeYIeS9w7Y9zr+8zrq0h8F03k3Crf97eK"
    "lZwZo53G1+B3GcVYxluzFIFmFIAzHi45RWtzwOHhAbqcF/ccchOhzetkNMnuGM4XFGkH1+hKL6uxZPjcibti5LKOV1VRcKDKdsKilAtu5GqIwu9iHf24xe0Y"
    "o3YpfTFHiaJKZEu8yYL+fA/PgeYLIPOgHaUFVSKq2lHRvoMvuxk57GgKpMyusKLAM5NoS4EqPmdB9zOlqIs5SijDnMbj6BR4vqrcDBkyNy2lVLlDMGa1lrrI"
    "pAhoW84zrqMrPdRCl+ap/k6lmLLnvUne3eucA2A6dg4HayzPH2fx5LOsnD3MqLuQ+FwIErJmvU4Q1EAESfeBJgwE9ZogFJLBKOb0Yo+Dx5Z4/MA8Tx9cYN+x"
    "sxw9scriWi8hgEpJvSZp1ELCQBIEMuVDqGIaW0q0VTbSZ26gUhgkRIdvZM6IDdTMLAKy619ktZjQejGQzNawtMZPjswyKzIy8mKuVBDOErahdrtQcjq5VNIp"
    "jcC24vMmBU2sNMORYhRFRHHCcZps19m6cZKLds5xxe6NXHHhJi49fyM7Nk7QataIFfSHye8k0+BE2SMFRNGI4bDHsD9Ai4CgMUVnbjszGy9gYnYrjdZEfl+S"
    "UbJEZookgT82Am2x292MJYs86TugxzQFY2P4PKnP7j3GzT4xIDGxjmeEpSzEIN1XyFYtVaLJvxOikiBf5btR5g8KT5fkuWaOy7J1pplpc/lI30FZtC45Bfu8"
    "gUQV0iqq79hYZaCvcbf5ConwQagiQ8gYgblnt6bahh3HZVxIaewJ66/NUnhbFczjHvJWup9DPvHN5ViHqawrfP3Fel1uFlutVKULnRWiJoS38qYK7fAcvFTE"
    "vq/7Ws5cVjj1CusUHDmcWOXCpstEsNIyF9UscZOQmxcnngp8vbA6td46OgfY1IWfLV8E6UDPxve2CguP/CUrxKQoSAdKK1A6ccFMf7/fXWT59GEWTu5l+exh"
    "hr0VIKZWq1OrNUAGicU1CQGyUa/RbNQIw4DhKOLY6RWe2HeGB54+zmP7znDgxBILSz2GoxgZCGphQKMmCQNpz6yN7Aml7XvgwpgFAgM2P8zgcZhW+M7atZ4r"
    "bb+mmasis+YihacLO3VJIKV3IWXbgk5/NpGgGtbsuvwM4yBwooJ1rLX2H6miUAMIUeSoSJmNjASxihiMYoaRIo5iQimYmWxyyXkbufaybTz3sm1cecFmdm6e"
    "oNNsEMUx3WFMNIotBZ7QMXE8YjgYJtc3aNKZ3c7slovozG6j2Z4uxvUqSt1WRcl/p2yO4dkXfbLIyucmqxzHzzwrX88jRXX5alr7DM4oSSx9xb9wEe+KUSzm"
    "GMb1uTF+N0NQy4ebZ6ZVxY/wjpZ94yZK+VAWqrFOtAdVNAQvEmuMZ/AYcQlP5pG2HRms+y3Euiqpdfl5Do3BelbHoG9Wo6hipRHrQSgOQOjID8fZzZ574mJZ"
    "Dy5y4qkeM8ZImbx5V13APd60AfcBrkrt880bs8U9zr684vW01wkUe1zi8z5xkQ1zYzWosHmMPI72XJfD3ISJCmj7s1mdsCFJ9WNoZTdZ86SoZG+L6g7MnIdW"
    "zaddxMyynsdnDCYKsmhOeE66UBmE+ev11hZYPLmfM0efZnX+ENFwjSCoEdbqBGE9cctUMUIENOshzWaDek0yihRnFno8c3iJB/ac5LE9p9hz6AxnFvvEKiIM"
    "AxphyrVISZpKaXv8JhIyo8pCsEoEWDMsK/vuDok6X/9ZoS+RgbTWkVI6B3gjlayLRGKblndqlIwYBgPCsEaz1UAiGMaawWCIRtNsNmg0aggEK2v9xNchlIQy"
    "SN3RYxCpXDeQyX8rhQxgNIoBqIUhsaI0Ss3Xs4FYmYFvQqTdshFIVxwwxrUyilJJIWHMAvOKGieRDg8GCQoipGRmsslFO+e4/vKd3HDldq68aCNbN05Rr9UY"
    "jmLWegNGo1Ey3pESKQQqHhENkvstwhaNya1Mb7mQ6Y3n02xPlcYu2RjQBCtzUl5mTT1mbOLlZgn/w+YlQXo8QTQab6iUKCvyrCymfEyjvWqWYlSic26UcIou"
    "YYTC+Tw2SiMM73jVqU+0Z2Ski3GjLkmODVTOJEKOGXV49/7csNDxuTA5HlU8x9LIRVQGetrJ5j7VpAd5gvXT233ICn716VhfEt8o3kQ4xLgqzEv4FPl4hXEO"
    "pe7ow5MsKjyZIBY7vAJayytSbbtDmjro0kU5B1OzsTfGV+mW4oDPBfEQZaMtZ6yQbwZCl2WpJkxZSrHM5KIif+jKLqfaG1rlyyaxYFGqXWjHoztZMWjuDh6j"
    "m3V5JsZmYdz/sc9QJrFWCiELTka/u8TC8b2cPf40S6cPEvVXqdVr1OoNZFBDC0msFAJJsxbQbCX+GEurA/YeWeLeJ0/y4FPH2HN4kfnlPqM4JgwSMmgYSIRO"
    "CI9mSm0au2abwmUHoTTttnXZs0W4QVPSHnOl/zcMA7r9Ad21LrUwIFYxo0gzOTlBvVFHIpiZbDDoD+l0Wqx0B6z1+wz7Q7bNNbloxwYWVwY8fegMSsOOLTM8"
    "77IdNOp1Hn/2FHuOzVMLQr73hZfTbjRY6/Y5u7LKaBQTBIJ+b8jxU2c4ubBGIAVaxSwsr7FzbopYKU4srDExNUEtDMtkZlEUBvmhbCgAbP6Ns5qzPBdr1Knt"
    "w0/bhbVIPWaC9NqPYs1gGDEcKoJAsHG6xSW7N3Djc3bx/Ofu4NLzNzI71URpRb83pD8YgZCEMpEuqzhmNBwQjUZQa9Ge2cHs1kuY2bSbWqNVjB21Spuqc8+5"
    "GEsGFx6CZcapMqIZLFfpKktqVznnGy04hVzlQCB/xm11o2kt7+anecce63Doqnhf3s9fhe44cKGbyFt1dhS5YEWRpx0eo4XyWGiEJz/GNOXSSXmk3GC2Cp7H"
    "et8z+y7KOIt996sMlpzbxGIcmi1UoVNzLqC2pYNVF6Nq5FLB9HX9MUpkI6eyKxZz2XfCr1bJJJmeCyZlwTkR1ZVsFZnGKo4qbqioWsNmhSuwkiG9M7kKQmrl"
    "yeosbmWOgdwHmjFKnjFRze6mVp7WO7Cdc5+1xV/xQ8VVPgTr8ex8f5sbs6k48bQIEnVJPBowf3Ifpw48yuLJ/USDFcJajbDeJKwlJMBYaYJA0m7UaDbrREpz"
    "/MwaDzx9irsePcYje05y7Mwq/UFEGEqajZBaGKQogSbOQtC0GzEtylkY1ow2JV1myhiVBVqRmGFByklQhLUatVotATqyaykgDGosLC2ze9sc3/P8y5mo11DE"
    "jKKIux7ex1OHl6iHAS+74QL+6Dd+gjvufYLf/u//Sqw10XBAOxjx13/wC3z083fxhVvuYbLTIlYxn/of7+GKC3fxsh//E+a7I3ZsmOID73kj37r7CaLRiP/0"
    "829l35FT/PO/3sJNN13KsNfjl/7wk8xMtYlGI97zzu+lXasxTIuyj33xbo6cXaNRCywVjSW7NPd4Q46sNfa8PBsDkuWmlGWqOb9CeySCxkacEVMzuH44iukO"
    "R0SjmGa9xvbNU1x3xTZecu35XH/FNrZvniZA0+8P6Q+jhGwYJOm5Ko4YDvtEo4hae4bJDbuY3nIRk3M7CMN6TkQ2C6Wi0dJexv84Poa/BXb2T+HseRVOyF7P"
    "EG3g99lmRpY15T6rxYhdGf46OZetktDvlUON5WUltbcs2S6UN2JhJwmbKI72zSaKP7NOFZ9BlxAV5HNdaiArEZPSGWvWzqIUd+/ed585ndfYzfU5Gdckn8tY"
    "T4+37s/+Cb0ZGXistMfAH8JTZODJGynxItwv47FhNUcIwhNX7CIMFptHF929ySkxP4s7n6oK3TKTPa2iaFyB4byn2YVauIbX88Ts6kyTfU9omXFN8wLATvEp"
    "kVLdhzFjNVthS7rsBFq671Ywn51n43M0zHgU9qZgDQScJ8NeXz5nWe9oRiu0Sr0vgqSLXjp7hBMHHuXskccZrJ0hDGs0Wh1a7Q2o3Lpb0G7UaDUDRkpz4MQK"
    "9zyxn9sfPMLj+05xarGLENCsBzRqAa1GMyGbKp1EuefXxCbB5YoMozAQFts7r0ITfolOuuzk8EsOvdW1PjUp2DQzxeSGFkurPRaW+0xNT+SOuEEtZHFphR99"
    "4wt5y2tu4n9/4qs8dfAEoRrxaz/zWjRw3xPfYtOGCT7/rUf4o1+L2H9snmPzq+zYNIkUAYePneUrtz3K3oPHE0KohJNnltl74CT1sMGhU8vMTU8QRYrf+dCn"
    "efrQPGo44N1vezl3PriHv/77L/Gxr9zOi665jMmJFmcXVvm9//Bmdp+3hV/87b+hVgv5vfe8ncsu2sXe44/RatSIY1XaoHUCqxVnl/usZ8rDfEyicw6RFAZU"
    "ot2QuEJ5lkPDwlx9glgnHI+kTxFMturIdvLaJ+eX+cwt83zmlifYMNPmmsu28fLrzucFz9nJ7u1T1ENJtx/R60dooN6YoNkWxNGI5RNPMH/0ccLmNFObL2Lj"
    "jstoT23KzddUHCOltALpSq7DlJ09hadBKZ41M+3a10xpy1W32E90lZmIs0WmxlPC4KUIWZDPtT+GoXKM7c4DDBKzc5Lb+59zEJuumEWcgS8Mz0F8K3wwtCi/"
    "pzWqr2pCS6MTg6xuqszMEbBD+HePYOFTBeH/ftrzGiWCp8Ot8QVNusZxGvf7jOfGaK0Jiy2/AtFwr7vpRWEyZ8fNbipULN50CqOSMvkNWWhWEYesvQe+EBi5"
    "En6Nedlrw1dl40gGsTNZxsZQF7K/0maQwg95p+bzuXBwRh9qMjagzudvXyrunIfYUB+Z8jnt7APuPbYaHmnyPkqtpVNWaKr86wukSTvsfFHWsjvXL+FYFGFo"
    "g94KZ448xcmDD7N05gBEQ1qdSWY3bEYjiaMIrTXtRki73mIQRTxzZJE7HjnMnY8e57Fn51leGxIIaLdqzEwkCaVKpXkgsSo2M8q5QuYCl/l6EMhA5JsyiGTs"
    "kl6n5aUl4mjEhg3TDIYjRsOYLRsn+aV3vYodW2Y5cPA0c1NtXv2ya/jCrY/wPz52K41WHYFkeaXLq268lA/8+rt4zTt/l6f3HmKi02AwGPDL/+VjXHzBdlqN"
    "kHg0ItQRp8/O06pLaiJ1CRUBtVaLMICJZi2/MUEYoEicUUOROB6cXVhCAds2TLG8soIWGjUaEEy3aTUafPv+p2m3GkgV8ZLrLueb9+yhu9xn487N/Nk/fpWp"
    "qSkm2o10VJc5lBr7kNUGCwtylzLj5RSur6ZPdcKVEIUFtXvQaocFZdT02rL7S9agUmmIHYJ6ENKcrIPQ9Acjvnn3Pr52x15mJ1tce/EmXnLd+bzwmvO5eMcM"
    "tXpAf6jSsYug3p6iiWY07DN/8D7OHnyI1sx25rZfwdzWC6hnI5coRguNlIFxqAlr3xi3B5lkQ/OgFhVtajn+qJyVYXfNdhqs3S/ZaXM2l80+CMfxCarGq5Zg"
    "wUEdEjMyYW2SWdK1G2Ntukb7/IfdrDzhKQJ8PbmuJMG7zbE9WhJjXsgCYEzbgBKdsEI0UMVbdF3AfbwVZyyiPXrKbFRWhXJkvxtakL3QpUPZnHsKF8YyP6jL"
    "IXD0/VUXwJLzWGMODMdNYeUIeEcU5mv44C4crwH81rd6XCfvSoYrRyFijBzNJlWWkR+dz6/HEWx90dG+Ktf6rlal6kL9ht89ZYlq8WBU+wAIA161HVV1uRso"
    "zQwdwxwDlclJVjYuabHDtNYIGRAEAVopFk4d4NizDzJ/7EmiwQr1WoPJySnCsI6KE9VBoy6ZnW4zijXPHl3kOw8e4dsPHeaJA2dZ6g4Jg4B2XTI3kULfOiFY"
    "Cu14kaVJqgVXoPCawIoilwRhQBzD8mqfUJAYWAmYmJpAKUUjhHe/7aW8+IXX8M+f+xY3f+cRpjpNjp9aZKpT5wdecy3XvPn9nDizyk8fWeLvPvhzPPjkIb5+"
    "/35mJptEo4jf+Pk3cfMtd/PEnqNs35wULfX6BEpp9hw4QaNRR2vNYDhirTegHkKs4hQNDBBBjVEUUa/X0UENIUMQMvGlSFUqKo6RIpGaRsMecTQEoNsbEMeJ"
    "4Xen1Uhgbim57YFneN8vfj9Hj57iE1+9n1anQbTSJwhk4sugtcOPKjrvfN2lS0NKwWikUs+MIPHIQBgohWm8l4bQCKfQzT1AUt5FLt3VuXtmEW4n8vA5nRJy"
    "VRynOTKSqYlmQsKNIm5/5DDfevAgU50mV1ywge+5cTcvveECLtq5ASkS5GMYxcigRmeygdYxw7UTHHn0EEeeajOz5UK2nH8Vk7Nbc/6aSnlHWbyA9xnynHDW"
    "s4p9uFnhk0L4oX1R7n9cYrjW5UbA8oHQ2YFueyuN398oFUrlLbhs0ugSX6sSwq0U73wcJzzkTqucscbSrtpRVHX4wiZ8a1gfFXcLR5ELvMaG3o2TKZsFhdUo"
    "VlhZuBQI82cTz6sCQcvu73h0J+keQu8bVFXPZpXkiYQXHqhMj0uC9c6vDGtdocsjG2OhCwNtsTZ5XN98YcD+/os7zrjMDPjRFhmtzGEx0QOTBaQ9Zinaw3kp"
    "m7VQnTRrjBe06XMxht9hMrJN0zCzUnWJRCa5rMre3eJ4WBJUZx5cMSfUoiCE2vbtwu48LRMplULeSRc4HKxx+vDjHNlzP4unDyAlTE5O057dlJw7KjlQJtoN"
    "pBQcO7XKF+54lm/ce4iH9pxieW1AIwxoNkM2TreTsLI0NEyYMexOSF/BNTL4BVjBowlKEEhWuwMmOk3e+NKr2LllmtnJNiLQfPifb6MmJUvLy5yYX+Htr7ue"
    "//Y/P528p0yUMF+57Qne+aaXsGnzRgY65N9ve4z5EwtcfuFOvnT3PkajiC1zHS7btYXPfflegjDhe2hEkvAqJa1GI1FupD4f/cGQdrOWWSGlB3DAMNI06zWQ"
    "NaSsIYMaCpnkkKgIpSLQcXLvVYQgUe8Mh1Fud5qtq6npSf7yY19n17aN/O8//yV+8JYH+K0/+2eOnl2jUZO5GaPGNC7TXlt5IQTd3oAtc5PEWnBmYZVms26H"
    "hekC6s8VbumNyp6XQAhEENLt9+n1+ggUtTCk1WwYfJCAwuRbYW41Zg+lVFqoS8lkp4FMlT8PPnOCux4/zOy/3MONV57Ha158CS++ZhfbNkwyjBTd/hAVa2r1"
    "Fo1GizgasnT0Ec4ceojO3Hls3X0ts1svys3FMvTOgBI9e4UwbBn0WGl6SSHh8yXCVrb5pJWiQsqZNy25a7HDN6BaqSYoz3KFz6nTRFjM5tRp4NzvlRegukpZ"
    "UaYImEeZxHViHctjLXFSvXErwmzuhR1ZacjYfcaI2nMO64oz7VzSyn3qUt+f5REW0kadStczPWhCV9UgRHmsYH54XbHITP2ya+Disl6tL2fOiYzcFStAK3vd"
    "3GREOfNHrIPX55xXUqQ4BFEfulEi/1R9d3dVCRcdSBZ2iQckHA6EdmatuHbpoprYky4+5d4TYfv3A5ajqImK+GZ22QzUZ+ErXPc8z8xXO6F37hVO/l7b3A0n"
    "sAqDAwEql6ZmY5PVpZMc3XMPJ/Y/TH9tgXq9yczsBoJ6DZ36LbSbNVqNGgvLfb5530G+ds8R7nniOKcWVggCQbtVY8NUKwkEU5oond8XBldGUZlBunkBUhwA"
    "Gaphb9KCUAiWVla57spd/OzbX8kXb3mQb93/NFPNGu944/PZvX2Op/afoBZKvnXfkxw8fJRLLtzCY/uOAZpWq8nZhS6NWoPzNm/m0QcP8H2vvZaz3R5f+vaD"
    "TDQC4sEI2ZJoFaVS1AgrGSMbgUqZynAFw5FiZmqSQCYy14wsGUcjwlCACBBBCDLI1TUqGqLjICedaRURoKmFgkipxMo9LQKFFIRIlJS8548/yc23Pc5fvPcn"
    "+be/eA9v+H8+xNnFLmFQxuO0O0JLxyhr/QFvfOkV/M7PvJZIw29/6HPc9uABJjrNfMyl040w8ctNk2UNAroIJFpIFpZWuOKCzVywYxPd7pDDx8/wzIHjNFtN"
    "ms1m7rcrBKBsImuydo3RTPrMxloRp+u102ow2U68PG594ABfu+9Zzts0wytvvIjXvegirr18K61GLUnp7Q+RQjIxPQvEDLrH2H//sxxszrJx11Vs231tbiym"
    "4tjynDFEIKUsC4sT4zswHKRSeOYEwui0LSWiKy3FpyjTlhpj/WLFE8DoHZ2X+XQ+xWPJat3K/jD9Zoz/zvdei0KE5e0xlndQdJ7CxjyNtF9nBIvjFm3Gj5vH"
    "ibBRfSu/ZpyYoOIcKwWfmqRSH3nY3ARVubiwmkxXqmsjHAaBxQOxuTbX47gPbgS9yyMoJb+aF0bbBk7WnF4X1pZCmBpkYxInvFfWqnSFb+6my7klthW7SSkr"
    "1CU+ZAftGUHkhG6Rz5crBRZpkJidASdKXYekWrKkKxAqm9EvfH5DpfmpmVCorRAfVQ42qrDi9WYGCEdpoOwu1zTqycyjEhg76fjOHt/L4afu4OyRx9FqRHti"
    "lonNOxEykYFKrZmYaKCVYu/RRb585wFuue8wh04soTS0myGz0220TtJTkyJDON2MoCSXSD27bTKasNj7ZtciBfRGI87bPMmf/ud38ku/+w/c99hBZmcm6A9j"
    "njxwmrnZSYLU12FhaYmjx09zxYXb+Tf9EDKoUauHLK4kya/vev3zePULL+IHXncTP/1bH2HvsUVmO3VGA8WZhTWOnV7kqku2EsdxMi5QyaEbSJFYpsuAWqsO"
    "MmB+pc9VF+1Mnv0gJJCC3mBAu93hxHzPsFGH0WCUdFRqhFa1vIvWKiYQAYEURApkWEOKMElXlYVV/9zcRv7t9mc49Esf5Ct/9cu84nkX8vefu5sNsxNFyqsu"
    "z6yz/47RNOt1fuHtL2ZyIiQIJP+/d76YOx86kATAUVhQ5+idZZac7CWx1hD1+R+/8y7O276JL99yL6cX1njeFbtoNGp8+st3cnx+hSBspChVEnyXuDErTGFf"
    "Lt/XphFd8izGsSIWie37ZLuJFjC/3OOfvvAAn/rqozznwk289sUX8+obdrNr2yxKx6x1E6+TdnuKTmeKYb/H6ae/xfE9dzG740q2X3gDU7Ob8+cvha6KWAX0"
    "ekNd/8hY2HlGZQ2AqID7iz/PbMCrft6bJOv7LL6CxJMi63p+iCrTsSpUGddGTJRJnWbsfcazKpnp2b9fFAUOkZ/yhRWIMm1Pm3klxUhZizxAIFdiOqXQ2Eye"
    "9ZD9Kg6KG7GRo7nCFhhYZGXXBCz9gqFIY9HHr0jbOrXSO8NEKqo68YqZmrfaNYsbYV56A94zRh66cnhXVJOigkCjPShM2aY2I1Z62MKep8ZKkvWQWi3nBF3u"
    "6MSYeZ/wPKjKKX6osB82P4urACsKBHKCrkZ7N5hxC3s9KV/FLTI6GmGNTjIiqFIxpw49wqEnbmP+xF4atZCpqVnCRitJHlWaRgiT7SYLq32+fOchvnT7Hu5/"
    "6iSrvSGtZo3JTj0fscSxymH4QvlkjsCyqqhw9ZQSIhUTq8TASlsrUxhJjclVCIKA5fkV/vQ338Ghgye574Gn2bpjI3EMU53kIDp5ZpkwFSYMByP2H13gOZfu"
    "IghriKBOLdAsr/U4s7BEvd3gy//yED/0xufzi+96Bff/9kcZRjG1ANZ6ff7uU7fwx7/+Nv7orz/LM4fOMrOhDRrOnl3hqst2I0TA0bOrtFttPv/NB/mZd7yG"
    "H/je6/jXWx4mFJrnnr+Rqy49j8/e8hDtRpDwFdSI2Zkptm/dwESjRhTFBEGAEDCKFbOdFlu3bKBZb6CoIcIagdb0hiNufM75nLdplv/97/cyO9fmwWcOc/TE"
    "PGeXugknQseGe6V2GqDimQgQDOKIsytdntPaDEJwZqFPFGsa6DwDJFv7SmmkFMX6Tn1Oums9PvHBX+a8Xdt59Tvfx+paFykVg16Pn37bK/ij3/xRfvRX/ge1"
    "qSZSJqm9yytd+sMBQitqQUBnolU8O44tewlVFSTcH6BekzTrbWKteWTfCe5/6jh//S/38pKrd/DWV17OC67aTacdsNYd0h9EBLUmMxvaDEcDlo48xNkDDzK9"
    "5WK2XXwTc1suQAQpz0PYJFGty4eoGJNNIrzhbGV0QY9BKbTHvFE7BHPt82XyogNGJ20gNGZxYTVxaQOWHILCTpwe03xpC4XwEOGNf9fYIshSYeOxMS8h41qX"
    "Elyt62AEg1qSVUsGno3EhRHixrr3CQ9dws3pKiFhpt+UeQ6nMuQcxR0TMGi+XvC+97/v/ZYrnUEsMaF4q6BwzTzcsYIjiZUmUdOTFOpfuMmrSmGrSnJJoTDQ"
    "k/RQcIVvubrIdGl0C4XckEZjq0+FVbEK7VNKOMmahjKmBI05vv8l8m0KP1pVu89ONrOZdha6hXZZc0dR5omIQixmH6rGoavLhFHz2omK7sOt3oUNI9nzR6M4"
    "MTfLQnWQzM2lDIijIUf33s8Td3yKI0/fho4HzMxtpj0xTawEaMVkq067WePgyVU++dUn+e8fu5dPf/NpjpxaoV6TtFs1ZOryWW5M0hGJNPlKZrx6cs3iOGZp"
    "eZXJVp3Z6Tar3QFSGCBHJi82FrjSglooef9/+H5uu+dJbn1gL61GHa2FQYhNnU+FZrXX58pLd/KS66/g41+6m6DeIJSSxZVVXva8C5iaaPAHf/F5bn1wH//l"
    "17+fy3Zt4BOfu4M4Vky0Qu56ZB+X7t7Kr/z0q3hq3zEWFtdoBpJrn3sh73nnq/nOg/tZ6Q3ptJvsOXiCM/PL/Id3vZrrL9nKi56zmxdedzF//9nvcOj4PK1a"
    "SH8w4Pyts1x/5fmcml9icXGRvUfPEoa1lCypePMrr2HD9s2oKOLJfUfpDjRhmFzrRr3O7/7i99NqaILBiJ9/2ws5u7zK//z4t+h0mjlqKX22+6LImBBCMFKa"
    "R/ccZbpZ48lnT/LH/3grK90htVAW/A2K+5Cts6xgXVpZ442vuJb3/tI7eMtP/RdOzK+yaXaCWhDSbNZ56MmDLK8OOHJygTCsMYqh2+3y8usv5nUvfS5XXLiV"
    "di1g36FThGGYy2+LcYM79xc28pitPw31WkCrERLFisf2neDfv/0Mtz90iOFoxHnbZtm6YZJYaXr9xBq93Zmg0awzXD3FqYMPc/bEAQjqdKY2JkZ2aQBnAR0a"
    "bqtynGbRPmCs57wKiXXVhA5KrceM203pvt1hC4esqp34eAqDPCGss0RiIxZaV3/+MhpS9smxigOBNYYvIenG9SiZcHlCTN0m2LwwPlfo7J7KHOTXOQqLcyZY"
    "LszOe5ryaltB6WRkUY5RsD67lJiycgvpdaYB7nsLFatCXJQZpzh+GMIjt9HgvcA+c6lS5eyJnS/NElMosnw4J/Cw2++XiDm506btRWjHxRvGjdrBQZxIaYX2"
    "eD0Y7nmurLgUtFV8Mlki9OiSJrwU1e5LFBT+KGVdepDt4LxibicsQqvl079OLoAv7ZAK5nP+V3J8NWxgl2nqpmA07HJ83wMceup21pZOMjExSWtiFhkExKOI"
    "IBBMder0BzH3PH6Cf//OXm5/+AhL3QTNaNZD0JpIKaPgySSo0iICasPhMk+5FYVaIooUW2Za/OwPvZQbr76Q3/zjT/Ho3hN02vVE0pn5aGhjnWex9Cju+th7"
    "ufk7D/Erf/wpNs1NoyIFQuU5I0pDIAVLq13e8qrr+Mv3/hQv/8k/Zn51RCsUHD99lve88+X84g++jJt++M9YiTTXX7qFW/7xPdz8rcf43f/+WU6eXSKKFb3+"
    "iHe++fm8/MVX0l3q0+8plgcRn7vlfvYdW2Ki2UjCzwLJ6kqPmckmu8/bwKg/5Mn9x4iimHargYpVahMuWFnrEo8iJibaaXcjEOkoRAsYDGImJycIaiHDEUih"
    "cnXLVKfODc+9gM3TLY6fmucrtz9BUKulbqzkM3TSzk3kY8d0T5EiV6j0BhGDwQi0plEPadTDtJMqnI6ESE3+DHVKEEhOnp3nH/7oF7ly93Ze/uP/lampDnEc"
    "p/H0ybhkMBrSbNSJtWR2qsVffuBnUaOIj/zrN1lY6nLTleezZeMU/+1vP4fSgiAMDbhZEUhBrHRuyGYW82QGWdgmZEGQ7Dy9fsRgELNj8xRvfMnFfP8rruCy"
    "CzYRx5rl1S4aTa2epBH3e6v0ej0aE9vYfsnz2XTe5cm6Vf7kbxtJLo+FXMMov7+Nsc9nGVbFkLnE/3Kl6z4TqvU4jL5kYi951eSvOCPkqn/PATTXct2allQE"
    "xQmjMUN7/UZcu/cyObUoospW68ZZYDSmZuqz8JJ8bU8ld/9WWllcHO/oykSUHMv3UrCbwfFzo+vdsyi0CSu6zJ71kIr0GGKlz9zLvBGiEvoxXKYqgodK9icZ"
    "7C98ZJhCnunSobODwM/HdoAwswvNtf9GTGqR5UoZLHOYux7o0U7gxBMUp/3XeEzyIh4lS/mKCuu+C2RpB/BxMnwZCcJCOewZryn8N0mmwuRE6CzhMyaJgg8Y"
    "DdY4uudeDj91O/2Vs7Q6k2zashMhBXEUUwskM7Nt5pcH/PPX9/K5bz7Fo/tOEmvNZLvBzGQ9VRCoFG4VHudBJ502GUSjVCIZbbdaCUE1heob9RrHzy5z9PhZ"
    "fu7tr+Ds4lqKTBRDJ6FF4fKaXqMggMWlLk88e4yXXXcp7UYjkZrKKC02FHEcEUiJ0gHNZoOnDpxG1hrs2DDNkdNHaE61qElBpybYtXMzP/Cqq/nMd57kwT3H"
    "ecW7PsQrnn8pu7ZNc+LMElIETHRCPvrZu/j4F+5lbqpDpGIWVnq0Wi067eSQBZ1cr6k2/VHEg08cRsURrYakEdYLMy6drPmpTgchksM02WAze/bkeWt3GkQK"
    "okGcTlZ1ihxK5lf6fPaWBxPCIzA90UzPZ215MyQXTxnPSpp/ozNJnqZVr9FuhAlvI5ONiiyinGLOIbOgtfTuqJgamvM2TrPvwDF6/S4TnXpKKk3D6dC0gkay"
    "MUeaf/7LX0MKeNkP/z6REDQDxbe+dR8f+I138pfv+0l+4j/+FdPTkygtCIMaWmnmF1YJayGtZj3JWXGJ8wkgZx1qiTw6MZ2baNdZWuvxt5+5n4995WFefu35"
    "vOO1V3HTVTuphwHLawOiOKLZmqDZ7jDoLnLgwc9xfO9dbLv4Jjadd2VSeMQqL768oWPokuV2wsnRRudaFNxFcWJ4pjivl/2ZdIsCc3xecRZY7peisAA3+SWY"
    "6BWO1b2FUvu787KHUVkVOFZNZyUzC/+o2xPhUTXuEJ4k5+J8ys4eMytKFCouXahYsuJaG+oehdPYaVtBZPHvHMRJG5Eldh2VvoIuOITCRDU0XnVLhp4F7//A"
    "+99vMepNvXAGSblGWFmomq84caCl7M2lh1wqrBvkMGQz/3/ThtfkaGRWwKbxmEOcFOmMq1icwrLXzcktwobNTG2xJaRBONIuB4oyxj74EBvtSHyd4DOTR2Jx"
    "TQx0w4p9tuS0jnmajxyU/nyelCqSA1YbqhDzt2SKMggjudGs8K2kxjxFs8gtMiFOoYVx/Ux5oUAT5/LWaNTnyNO38+Td/8LJ/Q8RSMHk9By1RgulNLVayMxE"
    "i4WVIR+/+Qn+4O9u5zPffIqFlTUmOjVa9VpeaOQEUBynYl1EnrvD41jB9ESDy8/fytFTyzSa9bSLS+5/rBSP7znEO9/8Qg4ePc29Dz9Lp1UvyFM2iSdfVqNh"
    "zEq3y6/89Bt4+MkD3H3/HlqdBiqOWFhYYvOmGX7g1Tdx/2OHCOp1Ltq+gR9+44sJiHn8maOsdEdMtRtICX/zsW9y8MRZllb7dJohx04v8a07nuDQiQVqtTAp"
    "jKVkcqJFq9kkUgnxcaLdpB6EyZkuDBmvUqBjalJQD4O0U8lGBdLiA2hECXpN0CizyNZWB58pkZqNGq1GnXajZix/O/G34O9kXBjDsjp99hQKFdsIASikFNYY"
    "r/CB0OloV7Oy0uU1L72WmYkmn/jCbbRajSztDRlIhAyo1WosrQ54xxtezM//xPfxjl/4M04urjE72UIKRa0e8NBje3n/e97OQ08fYt/hk7SbDfrDmFe/4DK+"
    "98XPodsdcHZplcXlLu1mg2xelBArXVl1gcbqFOALpKDdrIGAx/af4ovfeZL7Hz9Go1njol2zTHWa9IcRo2FMWK/T7HTQozVOH3qU00eeQYYNOjObEVKidFwc"
    "6DlyLHAy75yxhrByZoTP/4bCgM02tsCw6bcbUeFwB0Qpc87OYSiNBhwIQhsuqSYKUPim+MmrZRpAMT4pItwp1qDHZ6I4p/x7frbxZI2XFP4xQ2YJ4B7OVmCj"
    "sT9LYcmH8r1d54IKN2IioxfofE83kSphnQ/lcZM9Xsr2U8MjR1QTid1/D973e7/3/pw9K0Qlu9nU02pXaZL7Jeh8tiY8TmzkXYRxw9IPJI2CJasME9JiFo+N"
    "4S9q1t624kEg8nh067DPmcPFbM36Pga8JfOHTRueCkV3r5xgmjzIyGA3m7NCK7rZil0uHm5tXD8hZYlo5JrzCIPE6y5k6XA3rGtqFJPZ30trUWDLCB1ejnBI"
    "X6L0XsUGJdzMBmxDMa0Tl04pA+JRn0NP3sbjt3+KUwceRkpJe3KasNZAC0G7WWe63eTomTX+4QuP84f/cDtfuWsfwyhmutMgDBMDqMybA0O+mkD+6cakNAhl"
    "W4obXJ1BFHPhjhk+9Re/xjfueJwTZxdp1pORidCaMJScXVjkpqsv5LzNs3z26/fR7jRL0GQRPpZcgnazwZP7T9CqB/zOL76VU/MLHDp0gjAQXHnBDn7+R17D"
    "3Y/s5fCpFRr1Jls2TvKJr9zDtx7Yy2q3n0h1tebJfSc5cXaZhdUeoRSoOKYRSiY6TRr1enr4S6QI0GlnW2wyxSGSq9/TEVbWkZljwgxKleZaEAVB0uQFCCGR"
    "2ngKdFF85JtwOtZSKalcOgV5scElnxlPqJQQElS22RnPZ8bjEkVhbL5/diwNhxGjOObdb38N//hv32StN6DVbhAENWINsRLUm3W63S6/8rNvpSEEf/y//p1G"
    "IyCORsRxBHFEFEe88w0vYWWly7fve4rpyQ6rvRGX7NrMh9/3U5xeXOWFV1/Ajk2zPPLM4YT0GoYFzmeG85ndPEZAV/p8t5o16vUaB04s8cXvPMPtDx5CELB7"
    "xyyz0x2GUcxgOKJWr9PqdFCjVY4/+wAnDz9N2JxgcnpzgQCl9yrjJvm4DqYUXJgNpJMFYhzbVi6NrwHT5j2Rsszlc7gaQgvP5zL8KUSxnyGKfc869N193iVu"
    "mueUEOn6TRxlMUn86boyyq4c7ba+Z8lRu2hQS+FtLsfOsUTIm2rzNYURUCFkfi9t1V9x36RIJXJUNKCekUoh8dbVhnA4po4mGUGU75vZoATve//732927aKi"
    "CjO75OzDS/PQcgg52jz4hMjd+9w5UWlDcQmH4BAdzSq8YLFLBA7/pvhJUcHUNhe9yW4UeEk7ZvCcYLwbqDDCmMyCS7sVpQv7uSMJw7RMmAWIQeApmbxUfscK"
    "qZolMxMWG9pHOPVJqWyiqEjVB7r88zItANJoeB1HHHryNp647ZOcPPgQYRjQmZxGhnXQkslOnYlOnf1HV/h/P/sQf/J/7uY7Dx8i1qmBl0iC1rL2sETvMT5h"
    "ICVSJOiHlEk3a/FX0MRRxFSnzjtffz3f94rn8akv3sUg0tQCiNPr3usPmWzW+cHXPp9P33wvo1jlCGGsdYowGAQ0mfg+NOo1vnr7Y+w5eIK3vfZGvuemy3nO"
    "JeexZfMGPvO1e7n38UNMTkyAFBw6scTx08tJ/Hsc56FwSUhcjTAIrGAspY3CUMqisDDRLGGb1+UFoTYvnvbK483nT1LW/AthIqLO2MoNCjOtvozZuTAkyMIk"
    "MWu72HGVVslzJeyNOc/dSd8ndUltNkMe33uYnds28bbXXMetdz3JwuqQlX7ElpkOm+cmWVobEg37vOutr6DbG/JP/3Yr9bpExzFaxURxRKtV5+d/6FU8/OQh"
    "7nhwD5MTbZa6I37hHa9ExYp3/eePsu/QaV58wyW8+x0v55t3PsZwpHOuBirheoi08Ba5KZmpUMgQqOQaNOohrUaNY2dWuPnOvXz9nr30BhEX79rA5rkJhoMR"
    "w9GIMKzRarWJeosc3/cgCycP0ezM0J6cQwiJUpF1YEopLR8ca3xiocOicBEVwrsBiDHGUdnzr3UVg8MgmfsOuIqzwkS/S43ZmD1QOGKGkhGZsE8S4cm8Kokl"
    "3NybKvdN8zNps1EVpc/mtSqXAqcOzPewUg6aCRyY39mXfebWAR4Ojvm5tLCu0LrBx3k8vUj1mtrI0yghWG7F5SOc4LPRNl04dSmATJjQV0Wkr314ZjN/Wy6k"
    "U5Z2/pDk9rq6FG6mDW6EqLAA1cImXPq1zGVmulVXefwSbac2A6mxwEHtlZsJVxVCZjXrX5ilRFIP10WYFa3DdbEQGUtX7apzUqWPrjD5SteLUnEapqY48ewD"
    "7H/k66zOH6PRnqLR6iQQMNBp1qjVQp7Yf5ZPf+Npvn7vQVbXhkxOJIqNKFL5/TYVCYXFvs67BhEk8+TV7hApYLrTZHm1jwjChBipi1TW1dU1Xn7DxezcOMs7"
    "3/pSRK3GW37uQ7Q7raQrVDAcjdg00+LLH/ktfun3P8ot9zzJzGST3mBEsx4QxdnhkVwTmR4m2Z1dWFpGRUM2TrWJY8XC6oB6s8HUxERiNSVkfggopVISoEqR"
    "CGGsK2V5hQhDry/SMUe5kDfn4OSeL6SjE4FOL4Uy9tbCBlzjM4czCNEyKNJwM/6EsEXDkBz++Rxc2w2lzqWCJq1ZWx4O2uQfpTwuVzmX8wG0/X21hJXugB97"
    "w/O5+vLd7D98hiCocdmuDfzdZ+/k4Ml5lpZW+NP3/hQ3XXkBL//h32F6uk00iAgCzcJKlxdfcyH//te/xQ/96v/k2/c/w9TUJEjJHR/7Pf7m41/nQ//8TWan"
    "Wpw8cIw//b0f46Jdm/mRX/0rpic7yT3VirW1HlIKOu0mApn4gzgpqcJQFRTNSnJve/0Ba70h52+f5SffdD1ve/UVTLUDFhfXGI5iwjCxWeqtLTPsj5jZfhnn"
    "P/cVTM1tM3xtpENQrz4ktRFQZvIMVFpAClGOktam2NwwJytMCj37sJHzZCcu65zYjeNcLMadHWPOFuEaVIIVb68dxps+B0sAe8PXFTbiWcKu44OghSt98HL1"
    "3LEIeeOI9Xo5h9BEqB3xR4mAWhHZ4XWpdbJ1qPBhyteLyhlv7iUtyzG9kdqeG6qrFBO6HKZmjhIYs0i0I/kRpeJPY6bs5ZKqKsMabWtshXMBS1wi96Z5qk/z"
    "Onk/v69iFePZwtp5UKTH+MYl6grD6S6D3qxoe7MA02VWVTIn12NNeOzC0ZAHO34n2YuqOEIGiWHXqUNPsPehm1k+/SzNVodmZzLnDHRaDVrNkEf3zfN/vvIk"
    "t953gO5gxMREnVoQoOI0ZCyXa6clpjQZ3wVJUKPpr/UJAsFLb7iSay/fzQuuv5CZyRYf+J+f5e4njtFp1YmjGKEVS0srvO7l1xBI+OwX7uThm/+EB586xM+9"
    "95+YnZnIZbpLK2t86X/9Rx54bD+/+gf/SLPTYKIe8Bvv/n4+9NGbGUQRMpA5lygnvilFIDSxiolGI4SQ1GohWiQoiI3hpYW1SdIE28gqe4bS2XwOJuYBdgkf"
    "Q6mEIKpV4rwap4VMrAxytNC56262MAKZ/E9m7qSyKD4UBdlMpb4XGohiXWSTxDr3Fci7aSmQCIIAwkAihSQIZKHrz+XEhrpKJyQ4j2Of3YDYLUf+ZzpXNySI"
    "kwwk84vLTLXqXLZrC51Wg73HFlhcHTDRrrPaG3D5Bdv41P/8dV7/rvfy1IHjzE23GY5GLJ9Z4Mt//14mJid57bv/mMmJCVZ6I970imv5y/f+NK941+9zdH6F"
    "ViOkO4jYOdfhG3/3H3nLf/gQj+87QasZMooiXvOCSzhwdJ7H9p4gjhVTk20CKYm0ThFj4fDE7PZFygRF7g0jur0hl+zawI+/4Rre+JKLaTdrLK70iOOE94SO"
    "6a0tESvJxvOv4/znvIxmayK1qBfIIHBGntpv2OdkZ+TW8aZSBO0UDjZlbd1GWFfzNcpqPAeSBv/Bhz/Ju1SglOIvPAF03vNNWMViPpo0rRoylF+laxnDkgFh"
    "mWJ6VaEmf84clTsqRJfQL9yzJH0daczVtOealc3WymeuEOtQMJx/D82kUM+Mw1gcY2xptZN8WlGNlQQCZodvfjm32K6INi69lbPhVCJ3uqIKFR7UosoOmKoP"
    "UX6r0gzPkRZrqjsKk+wpxvBqhGmH7ojP3M5Q2MWot5r1ythcq3G3fkJYEi8BCXwrJDKosTJ/nD0PfJmThx6hUaszPbc1YfHHimYzZKLd5KkD83z85qf46j2H"
    "WO2PmOyETNebxEoRRcoWNOnC8S6D4WUYoBRE0YhASuLhkDe+9FJ+8z0/yF33P8v/+MiX+PjNd/Op//7/8L8+8NO86Mf+lCjGIBcn9OvNcx1edM1F3PXAE/zo"
    "W17Ms4dO8Pt/dTNbN08n2SZK8J37nuIn3vJiPvvVe7j0om0878qLWe32WBsMqNdqNmFOFJLNSCUHfBDWUqJqWjBIDKZ/ylFQWMVGytHMHTyTUYFKvrOKiGJN"
    "HMcJETQNF6vVApq1kMlWnXo9pNOs0WmFdJohM5MtJloh0xMtmo0anVadyU6deigS75J6SKMWEoRBQqoUxVGodCF7juKY4SgiijTdwYhBpIiUZnl1yFo/ot8f"
    "srjcZbU7oD+MWV4bcHZpjV5vxGAwYm0wYBRDHKerVUoCIQjCgCCQhIEkEDIfBeVeKtn1VYZ8TzurXptQcfrHSrNxZorRaMjDe46ggGajTqNeYziMaNfrPPTE"
    "fj7wwY/zvl96O3/4l5/m0MkF2mGNP/yDn+OCC7bz9l/+C2Q6povjEd//quu586G97D06z+xsB1ScaDWEQMeK87fO8sCTh+i0A5aW17jkwp381X/5Rf7+k7fy"
    "8NOH+NrtD7O02k3GatpGD7VwN86ESxYrqAcBzak2h04s8bsfvoWPf+FR3vXGa3jjyy6iUa+xuNwDrehMzACKxUP3MX/scbZf+hLOu/QmZBCg4ign4YOuVhqY"
    "Qy1zvKLdft6WqvonuuXdr0oq6+u+hRt9KzxnD2XyuqVqrCpG8vco25/ZrtslSMP7fdwQtNypU2P7cGh/Yq076tAV544ZUGqKAPGasmnv3ahqmoXTnZYiRNDV"
    "VIxcFmtUk9pw9hO5i6ewvCkEnlAf5y2lw9jVytTUF4MDbUl2tB/hsEiSfuKAm9dhFz1OzLJPoupKefK0U20lZOea7JQ1X5oV5sk+Mp8hWxJZE7Ir3UxRlob5"
    "iv20o5AOyFxwSKWVrVAqTLKO2JGACSPXRGdsZTNFUWungyn8R7J5szTVP2nwWRDWGHRX2PPAlzn6zF0EUjAzs5EgTLq8MJDMTrY5cHKZD37iIb5y535WeyMm"
    "2nVmJupEeXiaEc6UP0Da0pwLGbLWG9CswaaZSc7OrxIGIfc9dgQdKzbNTbHn1CJBq8X7/+aLfOZPf54tGybZf2yBVj0N9BAB9UDwfS9/HhPNNn/9iTu49Z59"
    "fOSP38Xps4v8zSfvYcumGRq1gBjN3mPz3HjtJTx7+Ax//o9f4six07RbDUurrxEGQTIlYYrAKQUxLOcNPkwgkEKilCSOI0ZRzChFKJRKOuFGM2Si1WC602LD"
    "7CRbNrTZuqHN7FSLrRun2DjbYXayxcx0h3YjpNOq0agJAqGp1wJqYVKYJB2YTJGKFLFI30elklSVP0syf75kMVImDAS1WgBSph2UTNEKxShWjEbJ/RyOIla7"
    "I1ZXB6x1B5xa7LKwMuD02RXOLHY5Ob/CiTPLzC/2WFzts9YbsNYfMkoLjSCQ1EJJLQiSILYU1UnkpsaoyTmQhJEwHCfynURFkn6XOIoRQjPSmpmJFh/97Ld4"
    "7Ol9vOF7rmPYj9k4N8HZlQGv/qk/YWmlT6fTYhBpts5N8LKbnsvvfuhT+foUUjAYjdi2eZJWM+TUwkoygIgjggBecu3F3H7PY/z1x2/m9a+6kQ/+7k9w6+2P"
    "8Ikv30un006/i865crnM31H2aC2IYk2zHtJu1th/cp7//OGv88mbH+HdP3A9r37BhWgVs7zSQ0qYmNlANOpz6OEvcmzPPVz8vNeyaeflAMTRKOFWaXcLFqX0"
    "VowuWZtkYYPHoLSyfl87HaLwobui7ABqQGRGOrTIC/LiGhV8Am1xLdJxoNS5ayhOx1+VD1Z87aqTz5SOuv4YNm9PG8TswnemwlbAEBcIh0uhHTKpVYhl+7ZB"
    "oNeuOoiyskTrcqRAZgNh83mKEak0+UYmf0VhkUizsybE4A0oi0lgvLhxM0o5JA6MI3wHrNfhQnsKUl2aaGTvoWyEPlU3iIIlaLx3zrx2+BpespGhKigQW7PY"
    "wiKL4hRI5ihDuyQgF93xGaMJ4SStu6MV8/MUVXzxwAgHnNCVtsR26q6fJGaUxE6hYZvKWImOWaWfFSDp+EQLOPTUbey57yv01+aZmN5AvdHMrYc3TLc5szTg"
    "w//6CP9yy9OcWRkw3akxPVknjkljx43OKYX9SWWvQUpCUyrR/UdxzE1Xns9vvPuN/Nnffp7jp5aYaEmePb7IH/2vr/CP/+0XeN5lO3nwob20GwG3PrCHg8dO"
    "Ua/VUComDAK0CJmbm+ZTX7mbj/3TLUzt3MTDe47w/Kt38qHffjuNVpt/vflROq02n/vGw3zw779GbzAkAJqNIJnRx8rR9as0IV3kJMdkk5RW9ydlosxSWhFF"
    "Mf1IEcWJgiQQ0KgFzE022LJxgvO2THPB9hm2bZxk9/Y5tm+aYGa6zdREg0ZNUA+S+xErTRRr+iNNbxDT7Y04cmyJ1bU+3f6IxdUeK6sDusOYfj9irT9kMIyJ"
    "Ik1vMKI/GBLHMYNRTH+YICfKOGiELFAUKWG606DdbFCvBXQ6DTqtJp1mSLsVMtlpMtVpMtGuM9Vp0m7V2LShyY4tHZ5b20oYpu4NOkm57Q9j1tb6LK30mV/q"
    "cujEEifPrnDo1BL7jyxwan6Vk2dXWFkbEsVJhHsYhtRqMh/V6JSgnI9WlDCSaG2egMgzIJICN1KK2ak2jzx9mIce38/k1ASjUcxKd8jc7BQTE200gtXVRX7i"
    "zd9DrDRfu/0xpjrNtKgU9FZX+NHX38TB44s8+NQBOq2QldUuz79qFy+5/hLe+Rt/y7HlPn/9sa9x4vQC52/bxChKivdYmKmrZux88oyKnG+g8+cjihJn11YT"
    "nj4yz69+8Mu89Jrz+bm3X8eNV2xjrTek2x9RC+rMbtzG8tI893/tI2w5/xouu+H7aE9tSGXSOiFVm0istveE3N1Z22Fk2kMw15URFsLi/GQFhZnKbKG95mso"
    "PF4YwvAWSr0oDEdPbfK7PF5H0j2nrPd0EDTHzsGHwYuqxlhTjFHG0AfweWB5Rz9GMebKi60kb224G5vKHaPQM8Y1tiN02aJCmwRY1+/DpUloCPPYYLTXCVSY"
    "ZY8oe2lQwabVWnvACI2yvCjMcYadyWmNAdzD3iJlYuuRoZRy544ltDOPNIsgbVRyojSKKIy/hMWZ0FZF7BYiwuFiaCNy3XVI1c5rF2SiogJWGr/KR5vOq0Yh"
    "ZcpWHZ24oEz0yr6Gcqv+XHMujBhokTLPNUpFSBEgghpnju/h6bs/y+LJfbQ6U8xu2pYHbM1MNOkOIv7563v42Fee5PDJZSbaNeYmk9FJHOsill6kOFN6cIss"
    "4E3FdAdDRrFicmICDdRrNQ4cn+f+Jw6yffsGRo/uRYgWmzfO8e379rH/2ALveef3cvvFO/jAb7yLD3/sqyydWmB64xTNRh0pgsRbRASsrAwJ5uZoNWuMRhGf"
    "/uqD3HLvPvbsP00/GqBjybOHz9Bs1Gmn8egqjlKjsYxTkXY+uoBn87lqECTjJKWIo4jBMGYYpfksAUy36+zeNs2uLVNctHOWi3fOsH3zJNs3TbJ5ZoLJiRaN"
    "eiJ3HQyHLHVHzK/2OH56gZOnl1lYXOXE/BrHznaZXxxydqnLwnKXtd6I5dV+UkiomOEoTgy+nGw6k9eV2xjnBbMtlUy8udLVGqsC0TQCAoNAEkhBPZTJ6KZZ"
    "p9GsMzvdYbJdZ266w8x0i02zLbbOTrBhtsOmDRPMTTWZmW2zY9sMN169KwmIixXd3pDltQEnTq9w6PgCzx5dYN/heQ6eWOLQiSUWVvoMhkOESCzE67WQMAhy"
    "l0qdk/ac4jvfwpL2K1KaiXYbdFL81Wo1tmxoJ5ybdJT0yhsu4ud+9DWcPLNAHMWsLi0zFBLd7/NTb38pP/GWl/HD//HDrPb6bJpqsTgc8u63v5LltSH3Praf"
    "OI6ZnZviwSePcNfDzzIx0cqlw1Z0gtvFWo2agSoohVbQbtYQosbtjxzm3seP8aaXXczPfP/1XLBzjqV0DbQnpmi02iwce4y7vvAMu696Jedf+TKCIEzGLDJI"
    "X1thLhKXQ6YyabQcE1Huibd380ssczRXpaLLPg/m1lW4gjpNneFtgdJ28jMGKTq7pkniYXGIg+GYrK3Cw0TELedW3L8u0IyCf1VuyrVnxKOds9UMwSv5MDkG"
    "a272C+455omX8I23TDM266wxvLlsvqqwz9cMJYlVrItq0vSdkNabqNQar+QrkecXaOsAVlaFl3amxnYlMnfJAqC3ZoNmN56n5GnteEoYcFRetCQHlDY6ddMd"
    "r0q5kS06KWzIylLsONbvJcMxj1+FcvXgZkUtbHRUuoVJPk4qV4y+WGeM0LXEMVMVn9OUCjoQpzaId2i7MDHTPq0NQxpkSB2jY4UMawy6Szx5z+c5/NTtNBsN"
    "2hMziCAALZhoJ1yAW+8/xkc+/zhPHDhDu1WjWQuJojglMhWa+WKeWZhHqVglZFCh2T7b4carLubexw5wbKFPs9VgrT9kdrLOb/zE6/nAX32OoNYglIKzi6v8"
    "6k+9lv/446/lF9/3ETZumOK3f/513H734/zqH32K46dXmZqdJkbxR7/yg9SIeO+Hv0SsYgId0x8OGYwiWs0G9XoDKUNKRCGtKVa+ScNOvle2GcexZhgpRnFy"
    "Hydbki1zE1y4Y5bLzp/lkp1T7NoywfZNE8xOtmg36yBCVnsjTi/0OL3Q4+jpFY6cWubYmRWOnljkxJkVTi2s0e0N6PZHjCKI4ijtVCVSJOTPMAzy0DUpTM8N"
    "QzdreEUUY0c7qcgOQtT5iMVMMc5TLhEGcTBOCs848RVRcUE4VWk3EgiZhJw1a8xMtJiZarF5wwRbNk6yc/MsF+ycY+e2aTbPTbBhssVkp0EYSkaRYnm1x/FT"
    "Kxw6scQTz57ksWdOsvfIaY6dWWV5bZiM+YKAeiOkXkuKPm2G+KX+Itp0PjYbKFm4korU8ySQkk67zouv3s0luzdxdmnAcKS59sqd3HjdZfzF332R//O5bzPZ"
    "rjPoD5idavLNj/0B3777MQ4cP0utFnLfo4f52l1P0mqGhv2/Q+W3kPwM8SsKpaJIsf1TApk8T8srfWan2rzjNVfwztdfxdxUk4WlNYSAIAwYdFdZWVqgM3ce"
    "l97wRjZsuzhVwcUJAdkyblZoLRFCW7RK4XRRmYFjJUnCWn/YDZ2F+NpaaJEW67YHoxm0iDOqTIjKublhNjYvJUHbFgg5Sma6xToqDZk5W+f+UcWwVLrKqXTf"
    "l4Z6xLYRF/7G0w29U8qyoTBj6rXn9001pDRRJ4s0S2GY5jTQvjOjsHbXhdBAG6MzZ6QvhEhksdpHtBHSIihZGRgWE9k2vxWOrBavrbbJIHdCj9zxjDNyEJbr"
    "ZiLfE7mla/pOMllPSnu8NxwkwJWguRwPX8aAG8HrJe5ovW6Qj8vSsBIJXWasaQtoxts74xuHEmA8+9qfKaPNsDltV75GYmymtrD8NRDJgZwy3A89fRdP3vUZ"
    "ot4iU7ObCMIaSgvajQaTE3Ue3XeGv/7s49z+8FFqYeKkGMc6NwATBoqjdWFGl5hAaYb9AVJqhoMR52+d5oO/+Q6uuuwCrvvB99NXkjAQhEFizPXLP/p6ntp/"
    "nG89sJ+ZqTaD4YiNM22+8pFf5z3v/zs+/7WHeN5Vu/jQb72dnVtn+Mi/3MG37t3LqfkuL7vpMh596hD7Ty2gIoVWEVJkD3PmcCtLrHzTgVYKQRBIlNYMhyP6"
    "wxFRpGnWAjbOtrlwxxzPvXgTV5y/kQt3TrJjU4dNsy3qNcFgGLO43OfoqS5Hzqzy7JFFnj28yL7jixw7uczicp+1XpKSqlFIIaiFQVJMSEEgDckzhgVythFq"
    "m85WSOfSRqP0rBi21U7gnVWoWjAZVmS1TjNEhOU6VoSe5c9U+nJKJVyPhMOjU2VN8oJhGDIx0WDjdIsNMx12b5/l0l0buOSCLVx03ga2b5pgerJJI0wIoPOL"
    "PQ4cm+eZg2d5fN9JHt93imePLXBqfo1hFBMEIuWyBMZooNjzzNmvyDxOMJxYgVE0ZG1llXYj5KJdWzhv2xyDYcT9Txxhaa3H9GQLoRWnzszzyz/6Pfzmz72N"
    "577pNxhGiv/6a+/kW/fu46u3P8Jkp0EUxfbWZDUgRhuXq8OM4tCA/oXDPwiCJHxwcbXP+Vumefdbr+ONL70EITRrawPCMOHv9Jbn6Q+H7Lj0JVxy3WsJ680C"
    "7TDSpBOJtrKaUO1xJC6KA1HahoUojwi0I980GzxlSoU1VsBanrHiIiHap1wxxyOe/dbLctU2qdORpAqtS5YQwuHpmR9FV54XxXURnoLD5WxoA/Uyf96yLXfG"
    "Ir7xuntdGGPx7pMFu/WBOULKpwaxUlp4VB5W5oXJ5bCm6sKj8DQLCVfeahiF6MKrvYDoVUkmKp2Fa1WhWpcOcWVOOjN9vnbsYR2EokrC41hIlCVLHltxC+oc"
    "46Ph13ML6wG05UgOUuP8e1HFCkPtm3RvRaPmkZXlVXNqYSHJZY+2HlzbSY1KpQTagLWlkzxy26c5dehhJidnaLQSHkMYhsxNtZlfHvHRLz/Jp76xl+5oyMxE"
    "Da0gNu2J89mfysHOIAwIwlrSmUYDts21Ob3QpVGXrHWHbNs0yR0fex+//+HP8jf/9i3mpjvoKKI/HHHx+Vv4/lc9nz//x5uZnppCSMHJ+SX+6b/8FBtn2rz6"
    "Z/6c1kSTeBTznh96KVs2zfCprz7IvsNnWB1ENGoBzXoAKkFUCmMhadt7ywK2lEFyHaNYMRgpoqFCStg00+SS82Z57kWbuObSLVxxwUZ2bJyg064jdBLKdfTM"
    "CvuOLPLE/kWe3n+aZ4/Oc/LsGktrPQaDGCUSl9NaEFKTyeFRGMupZK2rwqdAG8iQdhHBvIPIKugiXc+2/U/HWClFOZMji9JD4ToPCmOz1MaIT1ubmUU+1gU5"
    "3MxnQLo21MmXi6KYKFYM45g4SvJsGo2QDVNttm2e5pKdG7j0gs089+ItXLBzjh2bJ+m0GwgBq6sjjpxe4emDZ3jw8cM8vvckjx84y+n5NUYqpl4LaTVDailM"
    "rCyjEFE2hkrNzASKKIro9/uMhjFIyUSnRb1eT7k0I0Kh+PbHP8CXb32I3/rQp5mcmiAQAVKGxNHQeQ5szyFhchDMsa+weQqmSiwf76cGcQJNGAj6w4hub8QL"
    "n7ud9/zI9Vx3+XZWVocMBhFhCFEUsbq4SGNilotveBNbdj0nIZXGETIds7h8DVvd53hplPYdYTg2U5awlgLXilGx0qpw8nUVkk4/JczDU7ieSk4nnu082Ygo"
    "M1U0UGBt+Ln4CiJXWuv3AzHGlJiPov1dHCPPstKlJLYspgGY9AhnRCOdc0+4SEeJDOsqNIspRAE3yHIB4kHlC+Mviwyjjfk5pXQ87cwM80GuC08Zc7ucpOKQ"
    "dtwqT+cZCEZBU3oNozCyGLRFEFRBABJW1YvHD8NETIoGzBg9GEmi1lZpwWG2lber73aJQNrnbYFrXiQsIo9wYTATkjMeZK9xj5mk6ECC+dVL4XczW8cqNqVM"
    "u88YGSQqiwOP3sK+B7+MGg2ZmJ4DkUDk0+0mQRDyuTsO8nf//jiHTq4xNVEnkDopbIwDJzOcymyvk8AzwTAWDEaK66/YxeteejUvf/5l/MYffYwn9x5nYqLB"
    "yfllPvib7+QNL7ma5//IB4iJkUoRBpLFtR6//pNv4ut3P87eI4tMdBosLnf5vhdeyif/+y/xa3/0UT7+5fuJtGRlrUcUaVqtOo16QCAyZ0ojWVOlnzGt5hKQ"
    "I4HTtdYMRxH9/ggNzEw0uei8Wa65eBPXXb6V51y8me2bJpls1VBKcGa5y4GjCzy+9zSP7jvF0/tPcuDEMovLAwbDJPisVpMJaiGFOUY2Mje0p1PMNP3S4v5o"
    "rS3Qu1TI5nN0WaxJYeqXRJFAWmo+ygVF8e/aOC/sxFRtxSJlcGyp1bQyRjDfU5jW6qJQwowSFU8UJYPVdj1gdrLNRTvmuOyiTVx16VauvHgLu3dsYm6mjZSC"
    "ldU+B48v8sTeUzzw5Akeevo4zx4+w+JqDxFK2q0GjTBJHY7T00GogpCYq7VUnDdB+aaeInSx1jQD+PWffi2/9p4f4ad+7a/4h09+g9ZMG5SmVW8SBpJYRVYY"
    "kbCss8rBY9o6iLTliJlwnZy9hGIEHgaClbUhNSl426sv52fecg0bZ9osrPQRQlELQnrdNfr9Hpt338DFz/temu2pFO1ICu9sJIUrUTUKjgzdsNBiN9fNgt4N"
    "zqNj8lW2WNAW6uzumzjWHPYYw0Y3tOPpkvvCmMoS4aDFJYTZRTM0lTh4pgbVwhJMaJOTaKI1WlcJNS0OiMx4NRWuosIzZhFeWwsbjaTqjNHu+tKl3LGMTGoX"
    "HIb0ylc9YaR8and2hp0eOM7225WhujMy9yD25XC47GWdEr3c8JlxJBjhKToymZlpfoRnfppcJts/XmvbXKvSAM2YXWZzV+GJpR9nrFKGIc3jwe4+GPc5dDEq"
    "KPT3dt2U4yY6cUiUQcjy2WM8dvsnWTz5DJ2JWcJaA6Vi6qFkZrLJo3vn+ct/e5w7Hz9Oq5mEqsWxKh7wxKTDRmp0nF+P0SjivK2zvPNNL6IW1PnCdx5nrT9g"
    "ohHw8DPHaDcb9IYR52+b4bZ/ei+/8l//gf/92e+wYbqBVoKl1R6ve9k1bJyZ5BNfeYCNc5Osdvt8/0su57KLzuNLtz3KUwfPJoFCqQ25ihWxivKDQxjZPxlk"
    "KiQEQqCEYDRS9AeJjHLrXIdrL93CjVfv4IYrt3PRzhlmphoQaU4v9nj64AKP7jnFo88c48lnT3P01BIrqwNirQhrIfUwIAxFYRuukwPUJGSaJkHa6D5MYEE4"
    "LGRt2Je7yZ850mBaImdeF1pYTrvmBkWujDCD0ux1rfMOVGH4H5f3XFV4OZTwU9Px0ZRJCrPDMjZFbah90vyYSMWMRopoFDEcRYRCMD3V5ILtc1x5yVauuWIH"
    "V1++nQt2zDI7mchjF5YHPHt4kXsfO8ptDx/m0b0nOLvQRQlBuxFQD2Vq4lZsCgqVH+5Fno8ZfpmMDsNAcv1zL+Etr7qWU2dX+Nodj7Frx0bOnlnh2w8k6qk4"
    "jw/XFpfDHDPjemQ483mMvBY73t0Yy6ZjFrRmaXXAzo2T/Pxbr+aNL7+YSEv6vWE6ZlGsLi8g61Nc/LzvY/tF1+bqHykDIz27sNPX66VYV5l8eTZsX9opwuV4"
    "lPWchdN0ueP2n0fZ/fON2skN7IQu+xaVaAPajpMvj5MqUHVbAprmWukc3XKlwiVDMsP2wGxQZWblUIHAu8pNbZ1zjHEbtb1hhJuc7lwYoZTSwkzn00XQlc98"
    "pGQ3bkI+bhXlC4BxZDPmg6VLBYc2iJyiVHDgVsVKl63KPdCeb05nmbhoYXuTYEcZa2xLWjMELt8gdFGtmnHAvsVmMZs1XrKNGyFte8YXxFKXV1Bpveuwt02D"
    "nvKpoBNSqEyMgfY+cgt77vs8UmpanalURieYnWzRG0b8wxf38ImvPc1IxXTa9ZzwXWjUi4UqMxtuCdFoBFrRH4y4dNdGPvonPwtBnRvf/t+oNeuEArprfeZm"
    "J5NEzTBgfmGJ//UHP81zL97CC9/+u7Rbdeq1gBOnFnjDK29g89wM//TFO9kwO4WOFUEgOb24Rr1Ro91sFKiV0gZPIS4KaVlkTWit6A9H9PtDwkCyc8sM11+5"
    "g5dcfR7XX7GZ3VsnabRqrPaHHDy2xINPnuC+R4/yyN5T7D82z0p3iCDJxKjXJGF6PXXm0qkcrriwZdC+TdWxP7SN+rQwjN60dT/NvG87oLMc0mURQD35KPaj"
    "JGwDPaPztvk55c27PMd2PAtySrgHSVQFPFn4CRVjn4S0m9QESikGgxHDUYImbZzpcNGuzVxz2TZuunonz710Gzs3T9Np1lhdG7Lv0CIPPnmU2x8+yL2PH+X4"
    "2WU0IllrYc1QhihrhKVTqLmgXyX3o9sfUgskN121m+97+bVs2zDLr//ZP9PrD1NsSucIb/5bMgN8hAXf+5KyTflkvq9pY4ytCwuC3JApFAxGMd3egJdes4Nf"
    "fddNXHHBZhaXBwnqIiTDXo9ed5VNu67l8ud/P41WhzgeIUSQF1U2gqYT1ZfLY7A8fSi7XfmQXo/CxTxzzIPY+vc8BNTwivB1VOZi9BXIFQWHcC0R3OL8HJ04"
    "C1dQbBNKg/+nHf6k5YFqiAK82SuObbtrXmkSWy1ZrMtpzKwJKpAT+5wtu5cnpFFr1uR3mivxDXwqjvX85c1ZU6kwKCMCVuHi5Iu4pDaTMKnS8Y7wohPFWEgb"
    "D13e5WmM6HeXGFogOZUeI06SLg6BxjXb8gq2nULOd11dWBLhX8xeMM9QF1kxw6pQJRUwaTIfl0FAd+UsD3/7k5w88CAzs5sIG3XiKKJeC5ntNLn9seN86NOP"
    "8fShFaY6Mg9Wy9Usovh3a4EKyfLKKlPtBkIrYhUT90fceNUuPvnhX+Hdv/tP/NvXHmCi1eC1L7uaOx7Yh07VAWv9IZedP8c3P/rbvPfP/5kP/tVnoFXjvO0b"
    "+LWffCN/+fFbOHV2kWatTjYUCGthYqpkz/MMlVSahhokBMHhKKbbHyKF5IKtU7zgqu289JodXHXJFnZunaUWSOaXuzx54CT3PHqUex47zhP7znByoYvSikYt"
    "oFYLqKWvl/h0ZAWBsOLJLbicwhtCuPp+4fA0KYh8UggIgjwYTMUxcRShUtVKVmskaZI6kQOHtSTjJpEleGK9Ve4kW8DYulQYW2ss41Wlh7GKVc4j0CljRKdW"
    "8UEQpGhSmrmTKQ2cuHIM3lIh2y8OBIlAOWmb2uSqpEnIGQqCgChSDIYRw1FMPQzZuXWaqy7Zyouv3cV1z93JpedtZKqTZOUcODzP3Y8d5tb7D/LAk8c4tdhF"
    "SJJQvUCiYm0gFKb6xwgRDJJ4+OVuDxWPaISJlLwWOEnXadaJyFETmav9rNAxY71mIwyVUXNSQnbJaFsnzkumt4cQCYK3vDZgulXnF952Pe/8vqvQSrG00kOK"
    "5P71VpeQ9Skuu+ktbD3/qrRQ1qkKy0ZKs2LeDCyrcsn07VMlA7B1gl+Ei8rbG79ttezsv5mKUThUFHuvdooWrb32CHocwmEmjOqKoLtsW3DOH5egLrTVH1uk"
    "XZOoa9lU+M5QX/zImDPZHWmV7pdwbO7zgiODBQytsDQJNXjP93yRa1EgAlr4v4BbWFA5JjBRFGXJQk33ObfwMIOeslmuO5axja6wvOPdEZFwgtHyUYU0i2Gf"
    "MZorr6p4kKzF7CEUCVsSOvZ6mX4aHuVMKe8mX8g4IW1pJLQ0578qjTsXHHzyNp68+3PoUZfJ2Y2AII4VsxM1Vnsxf/u5J/mXW/cRhJJ2s8YoSsLGhEneNdxR"
    "BalMVARIFG965XW063U++tlvUqslhkNnFxf46J/8Ihfu2MLP/aeP8PpX38SbXncT//lPPsET+09Rr9cIAsni8go//ebn85//n7dy6z2Pc+DIaeZmpvj7z3yb"
    "+x/bz2S7ZQVfWWY2wu6OgvRzjaKYtf4ItGb7pg43XLGDV163m5ues4mdmyaQARyf7/Hgk6e48+Ej3P/UMfYcPsPS6ogwFLTqNWphkEPviVuqyGHbRM0lLa4C"
    "wuZj5LCw9uY3OX+YRlKnnJIoSkZDMk0ObTSbNNpt6s1W2nVqdKTo99borizTW11lMBgggoB6o5lvWM5Dn5KFPcRxMwQwnX1Hw2FScDWatCYmaHYmCGv1JKZd"
    "JF4Ho+GA7toqg7Uug95akuQb1gjDWuo5o3ICVTE5Mef2urRXiRSZMpV1HgJErgwDnRAhZVJWjUZxwsdRsGG2zXMu2sLLrtvN8685j8t2b2Z2qkm/H/Hs4Xnu"
    "efwY37j3We5/6ghnF7rUwoBWs06YKpTy0ZIlkiZ1b09kpYkk1+R66dIYCas9Mn2Lkp+IY8Vqrw8KOu1m4vaaXjt7AGbmy9hZH9l7BoEkVprVtT43PmcHv/lj"
    "L+Q5F25gfnE1NciTDAZdut0e2y5+Ppfd+GZq9SYqihCZ0idtLNyRsWsE5goT8O1LDmqS/06OOtipw64AouD3aYv8b54fSimHlzh+5FPyiHJQBAUl7yjpolLC"
    "9qywEewiZwzHjsF6LwoFmimD9cZQmCM5x6OknFprChncZ0yU4k7ccFITwMi3s6Ru0Fqn8zyhnYO4xPgtG4NVBpBVBJxRYWwiXIKjUWJaBYavsrR7v7zg8KIs"
    "VrCZcJCdkqKvMma5ckHaCHD5bdPOrtDW+wuOcfJdKtjYJcjR4w9iEkgtYUHmrZERBFOuRm91gQe+/lHOHH2MyalZGq0k4bQeBExOhtz1+Gn+/BMPs+fYErMT"
    "9ZTJniXHpp2x0aRmRmGZR4hCMtmu80NveDG7d27j1/7rR5id6hCNhqx1e1x6/ma+8pH/xJ33Pc2f/cOXOHJ2lZnJKc4s9whlzFovQgaC1bUeF+3axDVX7GZ1"
    "rc+dD+xhea1Hp1kjjlTa1QbFhUu76awIken+3OuP6A+HzE23uenKbbzqht08/zk7OW/bFLUATi2u8Ojes3znwUPc9tBh9h48w2AQUasHNBth0r0qndtSZ9e7"
    "kBqnWnWHO6HzqPmioDfVGhonTyeXIun8wFRRxGg0oN5ssnnbeWzbfSHTm7cxMTlDvdlAhKFFjk7svSOG/R695SXOnDjKyUMHOXX0SHKw1OvFmMDgBEkz28Mh"
    "e0kJo8EAKQM27tjJ9gsvY+P27bQnpglrNbLQmBwBUZo4GtJbXWHp7CnOHjvC6WNHWTp7BqUUYaORFIm6cAvFFS8YRbc2Dx+zQZHFJEgLo/Ew5t1Zjl0gSaXY"
    "iRqm20+cTKcn61y2axMvunY3L7n+Aq6+ZAsbZyfo9Uc8tf8U37n/AF+9cx+PPXuGfn9Es1mj2UzyclQKOdiJC6pULNnGfNqGpXU520RKwShWTE+E/Phrn0ej"
    "2eD/fOF+js93qdcKp1CrI1WmgVVZnp/tUWFNsLzapxkG/Pxbn8e73nQlOtKsrA4IZNIUdlcXCSe2cMUL3sam7Rfn8lnz8NTaHAGV8ziq+B7jkHK/NQPeBHBz"
    "bL9u8+ZOVJw17lP8+VQ12oO4CE+xSMXPaarG6lYMrzXCydazm8FV6UxacT6Xw0B1SW1qlcDWe7nnl13ECWUyfqrmTl6+g/DOqnLzJhc682l5oXIRCOMLqIoR"
    "j6/ic++gPSvDku4Jxhz2VRwni51vQxkFm9jAuqxUS/LYcrzJl+Z30XgZnGOpsOuTS+2quIA88muUHjAyCDi+/2Hu//o/INSAqbnN+aY/O9GgP1L87b8/yadu"
    "2YcQmmYjTF02C+5A/v7GGojimNXVNWqBpNNuUKvVUFJy+tQCv/0Lb2PPgRN8/tb7mWrX0dGIM/ML/MXv/TQvvOZiXvyjf8DEzFwSSqYVP/uWF/A3n/42Umpk"
    "EDKIFf1ehJSSTqeBJJH3YcyxReZcSuJ8GYSS4Uix2htSl3D57o286vm7+Z4bLuDK8zfSboWcXery0DOn+OZ9B7nrkSPsO3KWtf4wD0ULghTGVuXxR7ImbMPk"
    "xJgN29lV2QZ0iXOzQqZjlyyiPWfKZ8iuDEDF9HpdJianuODK53DhFVcxt2UHtUaTKBrRX11ldXmJ1eVlBr0ucerzUKs3aLY6dKamaHUmkWHAaDhi4fQxnr7/"
    "Xo4e2EctrBnIhsPQz8cFqcwxjhgN+uzYfRGXXX8TG7efT73ZYjQasba8SHd5iUG/TxyrnJBbbzRptlu0O5M02hNIKeiurXDm2GEOP/UYRw/sJ46jxBJfq4Ig"
    "aZou5T4ISfGYKV6KrB9hOBJr6/Er1D3J/2+nJCcHpEw5RnGc2L0PhhGT7TpXXrCZV9x4Aa+46UKuumQLE50WZ+a7PPzUMb553yG+ef9Bnj06jwYm2vWEgJkl"
    "6Wo780WYyINOSvHcBt90ZHaanEBKut0Bf/7rr+cdr7ueWMPX7niSX/iDzxDW67lnT65U0l6Y1b63OaqcjEniKGZlpc9Lr9nBb/7EC7l41xxn5rsonSQiR6Mh"
    "/W6PXVe9hkuu+950n9A5cTd/X/P8cVQX6+63omLvs/xHRCWhXvicoFz+gTMiwPGQ8BU3Lq9R++wgKs8+H8Lt+JiYHMR8TmYbcuVnPo7vhpu/4hNhjCk2TMO5"
    "kojDk4uDS9D1nM25SsWdHVnzJw+yUVJ5VB17Y+ZAdgXsX2zSsb11v4DJ3JYlciW5MQ0VPhQWUcbDf/BV4JWohzZrCTNF0UlaNH3EhB8KKVkZV3QC3utuwHD2"
    "aMlRLjgFXmHuA0/e+Vn2PvgVJqZnabY6jEYjpBTMTTZ5aM8Z/vQTD/PEoUWmJ+uJVDZ2g4IMxnfaTUWxYrJZ43tffDnPHDjBPY88SxxrJic6NDttpI74jz/x"
    "Zj740S+y0hsRqhHdXpcd2zZx9798gN//8Gf5H//vzUxvneFHX/8CXvXCy/nZ3/5fyUMlJVImTqYaQZzmQaBVAusanWz2cPZS4uCm2TYvvGoHb3jJBTz/ObvY"
    "ONtmrTtiz6EFbnv4CF+/Zx+P7T3FWm+YeDQ0QqRMiJ6xMj0Wld3NYaYFF5CArSbAYXRrBy4u/koauRoaQRCEDAc9ZCC54nk3cMm1NzK7cQujwYDFs6c5e/IE"
    "Z06fZGV+ntWVZaJhH1ScHMBSIsMaUgY0mk0mp+fYtGM7W3ddSKMzAfGIvY89xCN3fMtIdFbGui2UIAiZ8ENUzDUveTkXPvd5BLJGd3mJY0cOcebkCdaWFxkO"
    "Bg7nKkVnAkmz3WFqZgMzmzYxu2UHEzOzSKFYPHaIJ++/j0N7nyFMR2jKcFnEy663x3cY9soJz8BRHjjBW+7JXqiBdGpEB7GC/mDIcBgxO9nieVfu4DUvuoRX"
    "3HAh5+/cgBSCY6dWuOuxo3z59r3c/chBFpb7dNqJ9DozNkvUWTgWFnZMlo1MKidZQqCjmE/8yY9x8fkbGQxjlnsRb/3lv6M/inNJNVoR6zRgEWG7f5pjxdw1"
    "On12U3VWEApWVgdMtWq854dv5B2vuYLVXp9uv089DEErlhfOMrXtKq5+2TtotieJoxFBULP8j6QUVmvu816y9iw3f88wIsRFS6wmt6SzrCRKlvK1TOaSRzVp"
    "j+JtY8qqokYIUV6zTmy8WayYluSWc2cOsdiuspbsF7wIRrUaxYe0a8v3ptSUW4Hq2tMzl8P6BI4PhwVRCXEOkiYPI7bi92yvCMoQjzNnHftzvmoNLMOT9eS4"
    "JgHJJRppQ97HOSyiqvFKCXZzHyrHItrNJ16P/OlWnG7H4C9SBJR8VDRxHBEENXrLZ7j/a3/H/LE9zG3ZmXYwEc1mSD0M+eiXnuIfvvQUsYBWI0wO3NzRUtvf"
    "P98kkhFDrRbyppc9l/f+0g+wcabNg4/u4bPfeIAvffsx9h9foN8d8OqXXM2VF53Hhz/5DWY6DXQ8ZH5plQ/+zo/zA6+5nj/4y89z2aU7OX12mU994TssrvZS"
    "p9PUOjeFwnMw3TDtScAAxUpvgEBw5YWbeN2LLuGV1+3k0l0zhLWAIydWuePhI3ztzv3c98RR5pd7hPWAdiM0LIzJURIzKdK0AM82V5GSGIUwmhPLQ8WMxUvX"
    "sS5U+b4RsgaCIKDX7bJl+05e8Lo3sm3XbrprXY7t38fh/ftYPHuGYa/LKIoShUZOjM04EUFKik0luHGMUjETk9NcdNVVbNl1IbVanSP7nuLur34RrSLDc8K4"
    "1jJIfCek5PmvfQPbdl/MsLvGkX172L/nafprKyAkQRAkPAlhmmelyhytUpVOhBaCiclpNu7YybbdlzC3aRNCRxx44lHuv/UWRqMBYb2WZl2IoohAWA1SFs1Q"
    "iidPRyfZyE8bCcr5WjUUMlaKpiWmSN5XSkkUKXqDEbFS7Nw0xUtuuIDve/kV3PTcXWycbbK6NuTx/fN8+TvP8OXvPMOhE0uEoaTVTK6diotOUpvx8BYVQRvp"
    "sEVjKKWg2x/ytldfze/+wqup12p86GO38zefuoPJdp1Ya0vBVjRDWKShsqwzezuVX5MgTHhbK90Br3/BRfynn7yR2ckWC0tdwkAgwxqrS/OI2iTPefGPsHHn"
    "Jah4hJQh2nskC69CZdy+JzwEC9M3SPsIc3oMN2M9jpyofrlKsuo6zXElQdPwHTn3a1JwcQpjOD/50wTMxTkWHMJR0VAi5eJBRqi42yLjcGj/genMl8uzKnte"
    "Y1Z7jFNW+Lz1TcmPSUx3DLFct0/pkfVol6/gWYlGPpV/rlVxIbXHSz83pxF20e1a3ZYe9qrqSHx3ExSfkVnl7+QqnLTbTscgUgacPPAQD37jn9DxkKnZjWky"
    "KMxNNzl6epU/+ej93PH4KaamGgiRbD42W1mkSoGy2kwDQS3k7MIqr3nBZfzyu76XPQcO8cbvuZFGKLjniYPcfPsTfOGWe3j7q1/AV+94nMOnV2nWYH5hkbe9"
    "9kb+08+/mY9+9i6++J1HOHD0LJOtusEVMZU/sjDoEUmWxCBK7JvbDclNV5/HW196MS+5dicbZydY6vZ5bM9pvnrnPr5130H2H1tEC2g3Q2qpwZlSyew78+VQ"
    "FnKmrC66fF9siZhI8xmEIw0U+aGp0gPPdVBI2d8yoLe6yiXPu4EXfd9baTYaHDu4n71PPMb8iaOJhDkIQEWJ94nWKBWj4ij3E0EUcfRCSEQgk4TSeEQ0HHDe"
    "ZVdwyVXXUW+2OPjkw9zztS9Sq9eSn5cycRaUMk0UVbzo9W9h2wUXsba4wOP338OJQweo12p5sF9m/pVHt0Nqy54Qa5Ok1xpBGCYcH62pN1ps3nk+23ZfyOT0"
    "NGeP7ueum7/A8sICjUYDFevciVQZboegPVJyc/spwupkbvRkkq7tsY2V4WGlasq8wZRpxspwNKLbG9Ku17nmyp1838su5RU3XsiubXOEYcDho4t87Z69/Put"
    "T/HIU8eJtWKi3UxC6VJ5uTZI79rJXdJaWSTK7Od6wxGXnDdHo1Hjyf2nCIJanqVSOnBMPoL2/LkFh2pLPi0lBKFkaXXA9o1t/vNPvoCXPe885pf7oKFWr9Hv"
    "rdHvDtl9zfdy4TXfk/sjnYsnhdssueo+N4HWRAZM63Bv0+ZrpJ38EVwKnnmW6DLRXzjUAV2hBBEetMTbVHv2eG3lxnv8o6xKwi+UAIP64Sm2LPn0mKKouqjw"
    "HEweNaqlUvGRHau65SpjMC+oYBUsVOAQZcmf68legejkHUrJOdN8bvBIk9w6sWrWZhQLCTFO2np7jTVbE2OqvDLJBH+YUNX1PweeSQneKnXLKYM8jtMwKsnT"
    "d/0bex74Ku2JaZrtFlEUE9Yk0+0637j3CB/85COcXhowNVknSln1pturSTnTusjIMNE4hSas1VlaXOY33/0mPv/12zl49DSvefHVvOEV1/Gy6y4jjhUnF1b5"
    "6m2P8Id/dzMbpifo9bpcvHOOI6eWOHlmhanpCdrNZqLE0Mp+GDJiYxggBfQHI9b6MZs3dPie687nzS+9iBuv2Ey7Vefg6TVue+goX7ltL/c+fozl1R6tZkCj"
    "HqC1yJUlrqTMhJ3z0DbHUEdg8wVyQhdYPi1Z4WdSfgqSrcjROBXHaSEC/W6PK1/wUl70hrcRDfrsfewRDu7dg45HSJH4mejRiCgaWpRqpWNUFBmz4IQCKmWQ"
    "OscWZNL+2ho7L7mEK553E7VajXu/+WUOPPEozXanGKcENUaDAVfc8EKuedmr6C4v8PAd3+H00SM0GrXUNValuUYqjbQXRNEokWCm6o0grFFrtJIiKVUJyTRN"
    "N44VjYlJzr/iKjZv28bq2eN8+9//jeWFM9RrzXScJpAiyIs/G3GyieA6H1sZsjpDtmW6qVarFEyWvm0jkMhTkzWy1kvyc3ZtmeFVL7qI1730Mq69dBvTky3O"
    "Lq5x24OH+MzXH+OOhw+y2h/SadWphwFxbPpomHuLOXCx/RRkIOkPI7RSNBu13PTLB7+WiIDO10vGTiqVjitLRZWtp1pN0B/FqGHET7zhan7+B68FDSvdfk5W"
    "XVlcZMPO53LFi36QerOTjFjCWo6c+A4ob4ZV1dbpFggVQgZv1LsnKFPYHYJVJJj7pvlaVkprqbgTJQMwPwHWRUcc/yfhYUlre6/RPsNJR3Qg3IbZK1JwMEGP"
    "W6l7jubNk3Eulmga2TVVWfvhvHE56h3bVsrjDKq19p6H7mtVjmecjrB0MTwLR1Mlh8KR11QRRc4NWfAWD07nUOWo6FPSmGFYlpXt/4d/zMmUK13MDcnS94jj"
    "ZL466C1z71f+XxaOPsnc5u0IGTCKIiaaIZHW/M9PP86nb91Lu1WjHoZEKjZ8PLRhOuYjuKaDLmF2SJLeMOaSXXP8wjtey6/+4f8mGo2IRhHnbd3E6152DW97"
    "7U0895Lt/Mhv/C0PPn2CVl3Q7Q6QQlGvByhVjE6K5aFSaD/hR/QGEd1+xHnbZnjTyy/hzS++kMvPn0VKydMHF/nCnfv58u372HvoNJIEzZCIdEQUl6LuqIgx"
    "NwlS2jTQyz1djJJPF66aSZGcuzkhsy5dJlbYOlZE0Yg4ihESwlpIWGsQj4bsuPAyXvKDP86o1+Pxe+/g9PGj1MIwGYtEI5SK0dGI0WhIHEfJ55UBMgxQwwTB"
    "EALCRoOg3kDHJMWVFKAjdJwcBv3+Gpc/70Yuuvy5LC+d5Vuf+xd0HCOCMN1coNHu8Kof/FE6k1M8dt897H/qcZr1GvFomB7+SZEUxzFa6XS0IoijESpW1BsN"
    "grCRJLYEISIIcqv1BO0IiFUMCC54ztXsuOgyFo4d5O4vf4Z+r5soQOKYaJRcpyBFSWy/EHATV0s21qXtQzv2H8LJrSvki5bZlrG5SplI6QcDRa8/otOuceNz"
    "tvOGl13Jy2+4gN07ZljrjnjwiWP86zce4yt37OHsUo+JdpNGLfneWlUERQrHV0OkSbYaTFqedsYz1kjA4ggkxW8gJb3egCAMqYWSKIos2wtzLw9S25TFlSHX"
    "X76ZD7z75VywY4r5xTXCesKpWlk4S9ic5aqX/yhTG89DxSOEDNcniq6zF66rHlx3/G+41lKkntpjEBttwm2CPaRR7wjFKUqqij1dGaRWjs6oIq2WTPdKdg3V"
    "KlQT/ijRbD2Fk3XE6vWVlOl319p8OD1XwT+/MccoXoarIdeR0g/1VzmXCptE41ZOwpw5artzz879UuS66z7nU+743Ox82uxz4LCUK2vjdUwNnr1TVVY44x6w"
    "wjMh6Q7da6KdO6dVjAxCFk4e4N4v/xVRf4mZzTvQShFHMbNTDZ49tsTv//39PLJvgbnpBkrjIdNl911Y5Fzz0IWy/j4Ia5xZWOJX3/VqTpxa5NNfvo3ZmTZr"
    "vRHdfkTYaPDCqy9mrTdi79EFGkGS36KiyA4DS2WB2bcUaNb6QwbDiEt3zfG2l1/GG196Iedvn6I/jHh43wKf+eZebr5zHyfPrtJqSFqNGmidoDZgJYSKMdBg"
    "FftdlzZN4xpkz0t+gCRlRnafpBBEcUQ0HNJotZjZtJlNO3Yxt30nnek56s0OSmsanSmkFDx257eZP3GMRqPBaDRK7qsURMMh8WgIWlFrNNAiIBqNGPX7aB1R"
    "b7ZotNqE9SYEyeHcX1ul311FpnweVJwWFwE3veq1TMxu5IFvfY2DTz5Ksz0BQjIaDrjs+hdw/Stfw9njx7n3m99IyiYVg4pSw684kVE3m0zMztFqTyDDEFSM"
    "UppGq43Wmu7KCt2VFWQQJAVTHOXptZlMOIoiLn/By9hx/kUMVxcZDXqJ50h3jcUzJzhx7Cinjx9ndXEBAdQsPxFdOH8anasUJn/CISUaG1K54Sik39moyBqx"
    "ZiNinXE9kmK22x+itOainbO85RWX8+ZXXsFluzYRx4on953iX7/xOJ/91tOcOL1Gp5MU+UrpXM4rjG5cZAo+M7VU6ZJrsk7VNq5/dG6SbwAo7XrIa1/8XL5y"
    "2+OcPrvEzGSDSKVhaamU3EykBaiFgqW1AdOtGr/zMy/l+15yIYvLXVScFJjd1RVGw5jLX/Q2tl98A3HK68Cr9tPnxl2rgHorkRHnYNUWIUOPicMwkk7xx667"
    "+78v5qKSwDnOidQ64FOPJEfmagfHZWvENfWhXCxVof2iRESs5FHazb8vNRu/tfm4++l259rlV7gsYtzkPu1HONZBHISwO0t/6p5//ua7qZYMyVkkpne8e6N8"
    "IxXTaCyDvMszsPKzZCY3muOgrAq1DMuqRltUSImNREWbfSty6aWK43TeHHDo6bt4+JZ/pNVsMDE9RxwnpMCZiQZfufsgf/7xh1nojZjs1FCRNrgHGTdF5XJD"
    "XcE/yYsdx3o9jQsjEIr3/tyb+YO//BSLy6tMTbXTTrxGf6gJwzr1mkyzTVSeUGsa1GQd81ovYjgaccXuDbzjVZfyhhddyK7NbeZXe9z52Ck+c+s+br3/KEur"
    "q3SaIfVamHeC2jDqsXlQuow7CjOwy6ow8nWvx3RjwlfSConWimGvx9TcHBdf9TwuuPIqZjdtQciAbn9Ad3WVfreLCOu0Ox0OPfMEzz7xMO3WRJKWKknGIrEi"
    "GvZpNOu0JyapNzsE9QYgWFteJB4OsqyAXGUjkMhA0FtbpbeyBDpKSJlK0++tcck113P5dTdxeN/T3H3z52m02giRmEO94i1vZ9sFF/HQHbdzeM/TafZGlGxA"
    "cUwcR0zObWR64xZEIOktLTEa9hOL91aHoFYjrNUJ6w3WlpcZDQfpc6CJ+j2i4TAJyhPJOgibHa5/xfdSq4XEwwFSSNrtNu2JDkEgWV5a5NDeZ9j76MOcPHwQ"
    "tKbWqCd7lTKNjAzNkDaHKKbUrFCmlLYoYQccijSNFQetdB/VzOyr1x/RG0Ts2jrNW15+OW991eVcceFmtBI8c+g0n7vlKf71G09y9NQq7U5Isx4Sx3ajpkrJ"
    "nNJQR2lc3Dz5HAqtbZl2tqcHQcDaWpdb/+l3iLXiN//w49z+wFPMTLVzgnDyHuTcnOxwDQPBaJSgij/z5qv5pR++jtFI0etHBIGk3++xtrLIhde8jkuuf30h"
    "cU4LSrQzThACsU4BsD6q4ekbddGQuuh71atazsxmjWC8hsKOhzcPdSskzTfKKYkjPNzCCqsKbRhzZQiFNgkb6xQBlspW2LYPluIEN2BPeNUpPqTK4htppbRe"
    "hwdQqRipsub2fZAqlMTtCEsoiZ3MV0loqYTjfH564wofkUu1hGd0AuRunIlbsuEt74PtRHkuJ4Xv8xiySgft8GrBhShTbjSemGFdSF5TouBjt/8Lz9z3ReY2"
    "bKQ9McVwGNGsSYIw4MP/+jD/9NW9tFt1aoEgjrVx0Nr3XBfhqVacbnJwF52DJU5MCXphGLK4ssr3PP9K3vE9z+Mj//xVHjtwEhEEyJRACLII5UvnvkXnkBRO"
    "/cGI3nDElRds4cdedzmvfeGF7NjQ5sxil6/ff4h/+eYz3PfYCQbDiE67Rigz62cb+yl4GTJPr7UTeHVZ4WNs8pY/i8hkhx72vwPQiyBg2O9TbzS48sYXcvn1"
    "L2B6biP97hqnjh3h+KEjzJ8+Tn9lgUG3y4VX30S91eLwM48n2TMiIW5KKRkNh8lIJRrSbLVotFvEUfJ56s02zclJUIrVxXlGg0FaZMlklCEFMgwS+eraauoN"
    "EjMaDJia3cALXvcmhoMB3/rX/8Nw0EcgaE3O8Kof/jHCsMadX7uZ7vJSghikz6lSmumNW+hMTbG2vMja4jzDXjcd86TE1tSiPqy3aHQ6qCgmihOUSwrBqN9D"
    "qzgxZ5OSKBqxYcdudl1yGQeffpSThw4yPbuBztQsm7ZuY+t55zG9YRPRaMiRfc/wxL13c/TAs4T1esJNyP1OirwWYbgsewJPbb6ELvYTUdowRcVoWBe1f1qt"
    "ZKO/fhTTWxuyea7Na19yCe947TVcdckmEJJ9h+f53C1P8S/feIIjp5aZmmhSDwMipW2I3CpjTU8PA2U0XCMtEUBaWNXCgDOLq7z7B1/MH/362/nB9/wFl+7e"
    "RqdT56/+6avUWy0CWUsRukLqUIxVE8mtkJqFpT4vvnYH/+UXX8LG6TYLS12CUBBFipWFM2y98Aae89IfIazVUXGccnds91GXuO9FLrQ5ziyP8K3xmeOu6Y40"
    "lM/6O9kOynoC7aDTQpwTVcBKY/UhFGPOJzeYzdds5mNdz6QB433wGU+6SL6u+HdfQbHemW78E7zvfe9/vzffQ1ByGDWjj/OgKCF8pI3CHMTJWKiCaGx/ds/i"
    "Kf2dP+elDI2J8RcqGwmZ5BohrBuUS/nc7+K+hfugCOMyCmz3Ny+cJvzfx2EgV7Guzc+X/VpWbMggREUD7r/5bznw6K3MbdpKvdVkMIiZ7tRY6sf81l/fyefv"
    "OMDsZDMRfOpicxYa22Qnq6wNBrm0vrsoZR5kyEC2ZJSGyVadS3Zs4I5H97LQHRFKYcj0CoWREIVSIwwEw1HE0mqfC7bP8Ms/dCO/+ePP54XXbKcXxXzmW/v4"
    "wP++k49+6XGOnFym1Qxp1QN0FuhZSjrG8D6x56HCCkQTmMdNXnrkyh8clMkkQJtQfPrzgaTX7bJj98W84gd+hIuvuYFhv88zjz7MI/fcw4Gnn2b+9KnErGs0"
    "Imw22bhjN6Nhn9XFeeJolKBrsWLY7xEP+uh4hFaK0aDP2uoy3ZUlBmsrdJcX6HfXaE9NEzaarC4uMOwPiKMh8WiQjnYkQRAyyv0yEkh8NBqydfdFTM1t4Nje"
    "p/7/lP1nuCVHdfYP/6q6e8eTw+SoyZJGOQuBEDlng40x2GCC4XH239h+DNiAsbExNobHBJNzMBkklFHWaDSaoMk5nJk5Oe3c3VXvh07VvfcZ8XJdktDonB26"
    "q6vWutcdqM3NopWid3iYdZdezfzMNCcO7jeirYOirtQ7QFdfPzNjZ5kbP4/vurGXgJ3PIyw7QFK0wndbNGpVvFYTpTx8zw+luioYsUSHTmi33ze0BN91mRw9"
    "j/IVlco8E6OjjJw8wdT4KE4ux5KVa1izaQulri7OnzmN2wq4CWmFSWTMlligp+6uMIa1gsweYJKmRHbjiteXzmwRUeK20hpLQqmYo+kpntw7wu0PHebUyBSD"
    "vXk2rhni5qvWcOs1aynlHA6fmmRqrkEhZ2NJGWcfpQPvjGIYGbtaBjMRjEMnWdPSkjSaLVYt6ePbn3gvH/vvn/LNHz/I9r2Huf6ydbzieVdxx4P7KZYKbcTz"
    "CI2NzgilNF2lHEfPTHPXY8fYvLqfDasHqdRaSAG5QpGp88cYP3OA4eWbgzGh52Xk0iYX7UKoQ/u+l0Yt0mFy5iidDqoYkUnhzTbRJrfD2CDaaBFt+3NMFk9B"
    "XZm6tDPaH435hJEN1PFnRXYvShdUyTUSKdM3nTrD9QLfX6d8jNqIsW3Czg5qnPD9g4IjA+9mY0vFBVaA6MDOjVJFdds9WzgUTmRuUBq2EfHMMLlwyZcwtfIJ"
    "StIeQmdWk3SopLMJtoT8ERYwAos3qIX4FR0Ir8l5dOFwtdQD0oHVTKYS1xHz0PAzCcYoQbFRnZ/g0Z/+B1PnDtM/vBRpSzzXZ6ivyO5jU/zVpx5h38lp+ntC"
    "uWEInZpdRCpFNJWsG6EOMuZ0pHyAswS38ENbVoBQ/PTXTzFTbVFw7JRRVpC0mZCnbEvgKZitNFnUX+APX3sVf/vWm3jOFcuouS6/fPg4H/vS43zjF7uZmK7S"
    "VQryTFA6TKw14b0OQUkZA02TaKvD7yriADqdUuFEM3I0ac5SRp0Wxc9rIWjV61x5063c8srXYxWKHNu7h12PPczZk8fxPQ/bDkzYgtGtwrJz9A4vDSyl52fx"
    "Wi1QHq1GDa0C1ECHhmfRZmtZoQpFBpwLz23RO7wE5fs0KhWE0CHZtIVWCst2UMoLEbGAyOm5iiWr1jC8fAXnThxldnwMpGZgyXJWbbyEqbFRRo4fCQ7B8L0t"
    "J0fPwCLmpsapzEwaiiaFZTkhWdVPnnUpk4JPabTyw/CpCNyK1mAg5e0dXgRaMzU+hmPZ2LaFbVmAYm56irMnjjE3M0Wpu4eV6zewdOUqxs6OUJmbJZfLxahZ"
    "TOYTymhIo+ZLPgNaL2L2EAaHIx2PEOe7xrJn87zSOkBdLBEUHp7vs/3AWW5/6AhnRucY7iuwfvUAz71mLTdsXYnyfQ6fGKdSdykU7IUoAkYAZWIkJ7KjoJAj"
    "IyXM1+t88+Pv5tSZSf7kH75Mf1+ZXCHHQzsO8zfvfhUjY7McOjVJsZCLCbGmgZs2TOt8pSnmbeZqLX754FGG+/Ncs2UJ9YYfyJ2LRWrzE5w5vIO+xWsp9Qzg"
    "K89wJk2jEws2n0K3NWfZ/Vks1H5fUCprotmZ38nuySI9d29DKlKIUId9PFOMpAujdJGYSgnu1ODS4bWi9zGiHPRCZzgY3kCZKlnrVNOFkVpsEucTBCVNtI0W"
    "n/XBD37wQ3Hl3mY4ItJdWVYAItJJgNkKMWV9nu3ADZgpW+VJ82eNhWy6dybdssjWnG2s4DZkwIzfNeVgbf77HSRaOo30pLphLTKzKNL+CdmxUAdpbsdF0AEC"
    "NLaw1KI3F6evPCzLZnr0GI/85JO0qtOBvwYCoQT9PXn+94Fj/P0XnmC+4dJddBJJXuqzJPBiXPRFwVdxvobMEH+SnS0ZQekMAiNouT6FYgHbtsP/LjPPscAO"
    "ghuYnnfpLjn89gs38/+95XpefMMafAW/eOQI//qVx/nGL/cxOl2lp+xg2xbKN3Yu2S4nM+slkxyYzMRNua1RDArdpgQSbQ+zTsYmRo5MVN14zSY3v+w1XP3c"
    "FzE3M82eRx/k+P6nUZ5HLucEvBLfRfteIF8WApSi2N1HsdxFo1bBbTSCgDTPM4y0jLGTqQkVEsvO4bZa2E6OQleZyvR0OMZT4cHngQ7GF8p1A48LETh7rly/"
    "keFlyxk5cojJ8+cA6BkcYvnGzcxNTnL+1HEsS8ZeEbliF1r5VGYmw/1KhVJISb5UQhlEuOg6BzydxII7yvRJEIjg+kvboXd4Mc16hemx0WBEqfygUFI+lgxW"
    "49z0FGNnRxAClq6+iLVbLmby/DlmxsdxcnmU8oPmiLSnhojRuGTfi5Da2PQs1Y7pDl25jl/HeDDTjpLRERHxwFRQABQLDkrDjn3n+dVDRzg9OkN/X4FL1i3m"
    "uddexFVbllKt1Th8aoKW55PP2/HzmN59s3HqOkWuD+ztbcZnKvzVW1/Ea267mle+599o+S5SSixpUWt5vPp51zDQ3cUdDx+gp1xA68Dm3Q7Ropg7YOxnSumg"
    "2Jdw16MnaXoeN125EuWL2Krea1U5deBRSt3D9A6tCBQsoXQ6myjdCcFIIe4d0IqULL/D78cNZRs6ZVpzZzK8LoC+k5Erm03mgvli4XnXhoIkH7ytYLkQ2TSL"
    "bKRyTkRnPkx8rnZsmEXbZ07OXZ0pUmRHhCfe/wG7bVKpM++jM+KRC7CD4zfJmrFkWf6GmUw7oTSRiiZ1nuhwMdPmLxhx8rqTjexCs6UOCE02c0XrDt8xVYyl"
    "Q5nM9Zk1t9O0B05l0R/dYWHrLJSYCrXTKbg2MnqyLJtzx3bw5J3/Qy6Xo3twGNf1yNkW+Zzk37+3m2/96hBd5QK50DExJpgackKBQEfBbiLioUTqIRUagAk6"
    "OX50kvSJREiPFEGwVZDwKZM00sAxG0vAfLWBbUtef9tFvPkFG9m4eoBGS/HzR47zrTsO8NT+EaQUdHflUArcMKsjxQPSSWqtCZHHs2jjIdSarO6pnQhsJjAa"
    "pOMU2p7hLoVOU7j1Bre88g1cfPNzmBg5zZMP3Mfc5Bi5fD52/QyDWYJxggqKNtd1mZ8Zp2dgkHJ3L7Pj53E9F4EOlBhGF5Imkom4apRCUJkcZ7i8GtsKslNE"
    "1O0LgRcmxkb5LkgNKKQlcBwbaYXha9FjEIXUhRJYYtdZRW1+Bq39uMvWgJ0vYDk5/GYzcKDUHsH0P0Ba4k0rvBfK17GcOjCPccl32WFwWCW8fzowYgtJwEr7"
    "SMvBcfJo3+Pg7p3U5ue55JrreMHr3sS9P/oOI8ePkS+UwkRV4kyTqEjWhmpNE8mYO6kZdEYIZ/B8YuRMpEh+2TyM1P6gAv8LIQX93Tlc3+Pbtz/NLx8+wque"
    "czG//dJLuOGK5VyxaTEP7zjB//xoB4/vO0+xmKOYt/F8jVAqra+J+FdGxpEGbMtiZqbCDZes4SN/8np+76/+H2fOTjAw0I3r+oHlfC7PiqXDnB+vhMRRiQUM"
    "9xY4fnaGQiEfyLhDQ7IIXQNiQnJ3d4HP//hpjo5U+MA7rqerlGe+2iRfLCJEgx13/w/N+hwXbX1urGBJIRUd5Ont8RTtdGzdFkvX4WeyPhsZf5MLezDqVPev"
    "zT0/64p9IXmvgUTFLthZfQOZQMdOiI0Zp0DWIcL8dtLA3zqxCYUhj02Sa2OaQWfqe7t6se1c1dhpZnCK5dmBoEMbo1iYBYTpy5GFlTIPlyJ9YyLijhCgI7Wg"
    "1CkzsviLCsPZ0uh8dCalr9MDHddhnRL9UotEp4o8sYBJTSqDwfx+hkOeyEY0pAX+7S6omU1NpQg/5kwtzTyO5MNRsXH86fvZcdeX6R0Yoljqwm15FAsOLVfz"
    "N//9GPfsGKG/t4j2w/h0YyQlIwlV+OISY2wlBW4Ywia0oFwuIEXykwnQE9loi4xaSRuFmU6hB9E1tSxJveHRdF1u3rqEP3zVVq69ZDEozZOHxvnST/fy6x2n"
    "EQK6yjnQ4HnZuGSdTg81DaBQRjQ0KftF0SEiWZtMf504jYq2CiMjkTN6JcuS1KtVbnjxq7j0ltsYHznJY/feSXN+llyhEOZqKLTyUJ5Ls9VCKwJfCSRaCGbH"
    "zjG0ZDm5YgnLslCei5UEZpi4StJBR5kVOiCIep5LfXaarr4ezp+aIpfLJ5uJLeLAr+Ah1GFysIVtW0hhhWmgIhjVhFLMoOAJbrcUFr4b+H3oSFJpSbQKZvhC"
    "WDE5VyNB+2H2DWihQCVdV/AzHmgrvt62kws4KvNzId9DBy6qISxv205AWrZAChtLWpw6egitNVuvu4nnveq3+MW3vsTUxAT5fAHt+4mxvLH/KUNhFSAFCWyd"
    "cWhLFaUiRdxL7z0RFyn6u8j4NhCP5jSeClJr+3oKeJ7iqz97kl89coA3vfhS3vjiS3nxszZy/WUr+Ml9+/nCj3dyarRCT1cBaQUhcULoVIKnFunxiu/5vP55"
    "l/Of//A2vvLjB/nOzx9heLgX11UUC3nGpyvcdsPFXLFpFf/11buwrIDgOzZb5Q9feyO5nMN/fuO+oDnRkZOMTsz/wuunlGagt8i9209xfmKOf/yjG1m7uI+Z"
    "uQZOrkhXj82+R36AW6+y6bqXB4WnFDFjMzuNUEZAaFtxsUDelzZaoo6HtkjOpWivkvGzlDiltiWdR7H2ol32ykLy3hSxKznTRGqhRAVGO+rQZrlhoDXaDMtM"
    "dcDSGCWb6szfUPWT9b7SITYjkoYDUy6t2x3zBALrgx9KOBwiDEATCxFnzMOShYiSIg0pdfCnSB2sHSCgJE44/EwpmNssCHQHklA0GtFtClERZJSnoCadUdXE"
    "B5M2OhTd4aZ0SGBNEW2j/y7TyJAQ6Z+LZt4JOVGkeS20k4Cy7xvPLkM43bJsDj/5S3be9w36hxeTKxRpuR69pRyT83X+v888wuP7xhjsKwQqlIzpmMgUR8Ig"
    "xkkp8HxNV9nmLS+9hk1rF3P41FhQHxpddNZfJMUIErSRuCL2uG1JfA3T801WLS7z//3OlfzFm65g/Ypujpyd479/tJd/+8ZODp+coqtk49gWSmmUah8HmmtN"
    "mnwt0pbRsSQsmrykpkKRBFemZpvxCNKAsYQWbTLaCHWyLItGrcLma2/k5le+lurMDI/ecxf1ynzgiWE72LkiTr6I5TgIIRlatoI1Wy5jzcWXsWrzpazcsJn+"
    "JUso5AtYUjI9fp7K7DSWkwsOfCmNQDWR3New8AkKORvf97n62qu4+fnP4/jRk9TnZ8KZfPp+B+NS8D2P1Zu2sGjZco7v3c3E+XMIIegbXsSqjVuYnZ7g3Ilj"
    "QaZNeF+V1ijPDefHUe6KzdLVG0Ba1ObmgvNEKZTn4ft+wgUyu6/wkJSxARgsWbmG3r4B+gYHWbrqIoaXr6ZvcIh8vhiMSWyHQlc3TiG8loDtOExPTuIrxdK1"
    "Gxhetpzje/eEKgmZgrHjsV7q0Em6LiGlUV8Y8H4HDNi0co8hcm3w5TLDmeAdE2UXgoDLgqBUcKg1XR7YcZrHdo1QLuXYuGaQay5Zzi1XrgKlePrIGLWmRzFv"
    "J6CayUEy9k1fa06dnwyJxi127BthYrqKqzSVmQorlw/zvU//KcdPj/GhT/+EQiGH6/sUcjb/+N7XcOu1m9l3+BRHRyaRIpRDx+GCOnUA+UpTLtqMTtW469ET"
    "XLJugHUrBqjVfRxHki+WGD2xm3pllsVrLksnfYsO5NCFTCbNcQwJUi7aeAadEW+RQfg7cUhS7tkZbqPM1DAm94JM1IUwENHYeMwgk5nfT8YjOZGS6rZ9j+x4"
    "xtgLMgqQtEjkGeCcdHMVGuaJADkWGWVkemSVriWsD37ggx/KWrFm/epFVh8juIBiYwG1iBAXnDe1yZlM1Qj6AnMzDAatMGB/kfrLlIsKg5wqFyITdaqtTEPj"
    "NgZuZvSWuXY6Q2oSMbxvEmdFitGbLszMk7D9mkW6dmlZHNj2Iw4+/hMGh5fiODatlk9/V469J6b4y888yvHRKr09BQMRCCB7naG5kRYkxQiI73l8/M9fwVtf"
    "fT3Pv2kDubzNPY8eIZ9zjIjr9Ewz9uIwLLsTx1qwRBAXP1tpYVuC33vJFv7+bdfw7K1LmK60+Modh/nol7bz8M4RnJygmLOIbDnSoyedSgrVHeTEuk3OnSSb"
    "kALVTLJsCJVr0SGIJ8Pojg4rKcL0XUGrUaNncJgX/+7bsSzJtgceZGp8lGKpGCtjlK9C/kOR9Zdfw9Dy1UghqVcrNKoVlO9T7uqmp6+Pnr5e8oUi4+fO4DZq"
    "2I6TkBjN2bFM4FMpJW6zxbW3PJtXvvl3GFi8hLUXb2HX40+gPDc+BIOsFBGPt3zfZ/WmLQwvW86xPbuYOH8ODQwsWsyKDRczOznB+RNHsWwrONSkDHxaDD6J"
    "Uj7L1m9m+eZL6B4cpDI9TX12Bk0QGieMoWMkUY7uopQSz3WxnTw3veilXLT5Uizbxm22aNTruM0GUlp09w3Q0z+ElBZuq4XnefH3lpbEzuWZnhgnXyqxfO1G"
    "hNCc2L8XJ+cEEfQR8S6WxYsOB5lOGcOlORuG2bA570YmKGdKuSY6NHKh/0c8tkreS+kgF6hczDE2VeOOh49w4PgEQ/0FNq8d4parV7B1/SLGJqocPTONJUVg"
    "M65SgorwQygEGtdzufPBp/G05ndecQNrly9hydAAr3j+1Xzun99F09X8wfu/wNhMlWLBYmq2ykf/5DW88OZLePG7Psmje05QKOQRKAQ+IkY50opGQTAlLORt"
    "Gi2Pux47xcrF3WxdP0it4SFF4MsydfYQE2NnWLr2slBRpOICsKO1QpvLNB32zgzP74JnkX7G86rj64h2BWQ2TT1Li8j+f2EUaGlGWfLnotNZ1IGImvq8Rnid"
    "aU8AF0hRD6cSbbzMuIBqV2h2ui4io1S0zSIi7voyfug6ZOYLZGLHbbC8O7Fws4YmYiEpTVb/LESahKdNSY5uqzhNmY5ZsAgCU7LosJMkFaZOmE0GGYwk2Col"
    "SzKgaaNDTo+YOsccd7qZypyv6szMLnvTjRtPx7lZNPcNOj9pWTx171c48fSvGVq8HCkFrZbHYG+e+58c4R++/CQuIiCHegoZmo0FVWhizy1CUqSKSH3GqvY8"
    "j+5iji0XLWZ8Ygrbsbjm0rXkc7nQjdQMBDEO/A4eJdG3saWk6SkqdZfnXL6M97z2Mq5YP4CnFD9/7BSf/fFe9hyZoKto0deTw/MUEW1EG+OMZFaoUl1EchKY"
    "vA0d/SMzfouyYHTbRmFaV0cwqAzTYJNQrfCQVzowsfI87HyBYrGLG1/0CsrdPRx+ejfnzpyikM/RqgXmVjoc0yjPZabVpFGpgBBUpqfxfR8VIUwCurrKXLTl"
    "UhavXsu1z38Jux64l9r8HE6hkHx2HX/NhBeFD1px+Y3X0VKaZrXCwNAgA4uWMnH2dMDHCQ9/ESmOYk5VMNJRIbkzKk6ktGLiXey4GYSnBDCr8smVusiXuxlc"
    "spRCzsEqF+gb7GPizDGcXC4mjCY8h9CPRmuEtPCaTcp9g7zxPX9M78AQj9x9JweeepzK9GRi4y0gl89TKJTx3FYAy1sO0pIBamvbAZLkOBw/eICBRcvYePk1"
    "jBzax9mTJ8JRpMJycti5XDo4EplR7WkDxxBx0F42h0UYkLnQIu3pYVYf5hBQhCR5k6OlNUokOUWep8nnLKSwuXf7CR7fM8Krbt3E7758C8++ehlb1w/yywdP"
    "8Nkf7uD8VI2+7nxIClbGfhP8UwrJosEeHtt5kr1Hx3nBDRdz8w1b6O/t4eOf/yU/u2cHs9UmPV15xidmeeWtV/CuN72Ad7z/szx16ByDvUV8z2N6Zg7ltUD5"
    "lEuFcD+IzAJVQmT3NTkniE34288+yuRsjTe+YCPTc01QinLfEDPnnuaRn3+G6170hxRK3Sg/st1XybgwrJ5kNuSNDhnLGbJmm7dUNqLD5ER0iFnvCPyrTIOa"
    "EhpkHLJ1e1MaU/NCD6DkHBApozETWW0PrdMJomae50T/bngP6QR5iUzcLtjUpwiuJMGScZG8gBtzZrSSksWmOmtD/iNMbW92XmkUG9FNlwZnI45/7qCXTh1C"
    "WfdPU4ZkvFakXxYdGLvaLICMm9s2vlmgUjYrw0Smp2MFijBQBpO6ISK/kQ4Kq0Q/bdJfO/uXCLKhroIsHz57DYMZYgBdbbvj85zY+xCDi1fEOv3BviL/+8Bx"
    "PvyVp7Bsi5wTIQMZSpXOXtNg95NG0phGYEnBfL3FisVlrtu6imKxwHd+uZtHd50kn7fjgyKJo5CxGiF9f4PRi5QWM9UW/V0Of/amq/jTN17F6iXdHD49w79/"
    "azuf/v5uJubqdJcdhBbhARNkVHT0phEd507JXDRSI5gcEjp1mjI9GsnooETGMwCDRNtqNLAsh8Wr13DJ9Tdz5bOfx9XPfQFDy1fSajTY+ehjeI0aXquB26zj"
    "tRr4fmBNjg7Qptr8LI3qPEIEhmy2JQPERPtUZ6cZO3uaVqPB8IrVLFq9lolzZ4NO33bat1yRHF5us8Wq9RtZsf4iLAFeo8mjv34IaQWFg+97CdwfFsee57Fq"
    "08UMLlvBsd07mTh3FiGgf/ESVm7cwtz0FOdOHg2NvEKre6XQvk+xq4fuweFgzFEu0zc0jN9ocHDXDpq1Suj3olLR8sSuoDpMdpW89l1/zMp167n9299i2/13"
    "4tarwZhfWAgrPMxaTRqNWkBCljIYg8U1pkL5Hpbt0GwGhMX+4SWs2LiRtZu3smzNGgrlMm6zQb0yj+/52I4TBpklG3m0PmTGXI8UT6hDKJoglfgpI/uBVBGS"
    "4WXFh1404g0bQ8P7ppi3QcATT5/jvm0nyVmw5aJhrtiyjJsvW0Gt4bL/xBRaawp5Q1Vi+sRoQSGfRyHZeegs9z6yn5/dt5MdB05jO5JyKUel2mL54j6++x/v"
    "5as/fIBPfO0eBvqKKN+jUqny2uddwft++zY2XrSMw6dGmatUQ7VViPDIROETjHkscjnJvdvP4vsuN1+2jIarUcqjWCxTnTnPyLE9LFlzGblCKeARSSu+ZjK7"
    "YS5kW5A6LWUbKCmyTeJCxo0LouoZdKKDJ4oI16FOJY92QA8yIYAJHSI6P2VovZ+CnJPxPyJeVxEJOstsQCTyWLN5EiwgDe4g142eByHMqQIpjltn1CeUxYqF"
    "io0sF6OjT5X4DccdLGhYkq2MdGZB6AuNZYyeQ7ShKiKWu+oOXbZY4HPqrJnWQnkqol2J0AYn6Szrt50jo3W7la8gyWRoq6tjkmzSsTzy8/9i5NATDC1eFhxM"
    "CPq7C3zp9oP8+3f3UCo62JZEqXYJsZmBEkHyIjUrTH9XaQkefuoEu/af4Re/3sdP7ttHzrFTiEJCipMpBCEie9mWpOl6VJsuL7lpDR95500876oVVBstvnXX"
    "Pj78xUd54uAkXWUHx5KBvbowSHnhBUqJ16KOMsOFiSXLmTWQ8mtqgwFVuloRuiMjGy1CDkAwNikVy2y+5npueMmruPKW57Ji3QacQpF6pcLc9CRnTx7n+P59"
    "qFYDr9lAeW4yekCF6ERQ8MrocygVoB9Kh7JVsG2byuwsjVqVlRu20L94GedOHA1cOY0EW5GRqAuhGTk9wlU33cDAwADf++o3GTl+gmKxGNp/q1jqrlSAgvme"
    "y+rNlzC4bDnH9+xk4uwIIBhYtIRVm4KRyrmTx4ORSuiZoX0fJ5+na2BRLHGdHj1HvtxDvVbhzKG9iHbJVugjEnS0Ukoa9RqXP+s2Lr7+Fu7+wXd48oH76Oru"
    "inkUGg1h0R34jVgZ3lOIioZcEa/RwGvWqc/PU+7uxm25lHt6GVq5htVbtrLu4ksZWrIUz20xNzWJ6/kBCRVoz9zQGRVQwoGS0fglHGnJbGNlFPKmtF2n+7lw"
    "X5C0GUhGaz0Ek0oFm/lai3seO8mRk9OsXN7HpjVDPOfqNWxaOcChE5OcHp2jkLeT7teUXYb/LOZzlIo5uoo5ygUHIQUzszW09vnZp/8PZ0eneccHv0q5lEMq"
    "j7nZOd7woiv56J//Nh/51A8Ym57nza++hX2HzlCtN8NRn4zHsTFxM3yEigWbh3efZ3q+xa1XL0Npjet65AsFGtVJzh7bw+I1W8kXysHoLeukp0mN+E3EWDyD"
    "11En/mGnvdaUkHb0qzCpBkYhR5tVQwcX1WeQti7INc2M76MCJJHiy3SxG++Luo1HCB1EEW2S1wz/pEOybtrKogMHBIEdg4Zat0l4dGrUI9ISWZE2UTI5DNlQ"
    "m1TR0CHkRhgOaloEn0UbsJQ2XitbPKTtp818hMjjXqTEICLrB6H1gimwwjhwVRv5yzy8dKrwyVbaaV6Djg2F0n+eKHdEKvJdGz9npI4qDyElyvd49KefYvTU"
    "3nCMErxGTynHJ7//NF+54wD93Xm00vihn4OOZ9UGh8Po6oVR3UfKIRGhCjpwFFVScs+2Ywgh6SoXQ4mkTgq/yH5cmBa6MpbTzlTqLF/Uwx/91lW86LqVFByL"
    "bQfH+PR3tvPY0+coFXP0d+fwPT9JSsSIEE/0Z8GcP5NRgFn4qLTLRiD3TK/plPhKZ0Lv4lFRu2W/ZcvAgAvBZTc+mytueQ7DS5ZRrVQ5dfQo504cY+zcCJ6n"
    "sHIOlZlJvHottowiRNMIxxUyYvuH4724m5BW2N0QSg59St1djJ89w8iRQ6y+5CqWrd3I8b07yBWKMToR2WmLkPUvtaZRrXBg/xF+9LVvc+jpvRQKRTw/WE/S"
    "dlC+Ck23RDxaCVA/KyWVi7J/ErKpjNNhFVAodweX2PNp1qtU52YZO3Wc4eWrcJw8rl8Pn1GVknpHuJjyfMp9A2y4/GoO7dzOUw/dT7mrFEqFlQEfi/SAOvLl"
    "AKSWYeqqihEk0EzWK+x4oIZtOzTrFXr6h1i0YiXLL1rP+suuYs2mixk5doTdjz/CyInjOLl8kFWjVGdg3cgy0hlkTCNDpZmIM0/SvxOuP5UQpkwJZMwpiTvo"
    "RHkSKdg8X2PbgnyPwz3bj7Nt/xne8vLLefNLL+Olz1rPFZuG+eJPdvHNO/YF7qZ5G89XafdIpfB0knIsZHAfrtu8nPe99UUMDvTx+j/75+C5Vi6gcet1XnHL"
    "FTzw+EEeeHgP5MC2Lf7vH72aP/rQ1ykU7cBzpS2vJviH70N/d57v3XeEmUqdD/7+NYicRavp0tXdR7UyyWM/+y9uePn7KPcMBYZ0lh2rb0TH0LAOIXA64XiZ"
    "B2LaJCxyGlbtysTsIaJ1kO4cGhRqkTb5a1N1ZJSTKYdrYZIIskqbtB1ByufDUM5p04pBA0K1pwLrtB1DijZhihcy3Vjb9RUSEeZPIdL5Yon5WcYTPNzH7KzX"
    "xQKCYzMqo/1gjmA+pdpZqhfQIasOY4RU0bOAxDZ+X90x3jalUDQLAxnJhrLvs1D1qzM0yg7M4DbJcEchrsa0907MwjQdkn7b/kWYrY8Q6NCcSSufR3/xGcZP"
    "72NgeGl4wEuKeYt//uZOvn//UQa7nYDdr0Va1K0xV2fC4Ijmc6ZRkRHWZJoJ9ZSLobNg4J8Q83sMl1FlzLJtO0A1Gk2Xl9+ygT96zZWsXd7F1Hydr/zsab78"
    "0z3Umx59PXl8X+N7KlHH63ZbLZPjojUpp1iNSjZOkUTDp0lsxvc3syfMgMLo75IwFTeSYQcwcb1WY9HS5dz44pezevMltJpN9u7cycGn9zB9/hzac9FaUSh1"
    "IUUu8L5QCiEjrxMRGz4FVvGhnI6IKyESAmf8XMnws0AuX2Dk2BEWr97EklXrOX1wD77nhqOKQI4ahW5Jy6Zeb3L9827j0L5D7Hz0EXr7B/BaDYQvcfIFLNsO"
    "ouU9k4Uvw8TTQAUTF/sqHOlJgQylrhH/wMnlcQpFlOfRrFVo1utICWOnj9A3OEjf4DDnT50gl8/HhYAO08mi9eJ5LstXrsHOF9h7z6/QykVrJ5SxJlPlaA9S"
    "GgNSDqSySpEkM0dLWimkzOE4wWHYajYZGznF6Mgpjuzdw8qLNrB602aWrd/M4PJVHN79FLsfeZBGo558XpPflmlCzE1baxUCGQbyZqLF2mD9k+S76NTINuND"
    "QRqljInSGlxP01128HyfT33zUR5+8iR/+babuOHyZfzdH1zHTVsX8y9f3cbxkTl6e/IBQhKqkeLiPIihRWhoKYWHZMNFS/nPr97OyTMTDA0WqVfr2HbwDOw/"
    "OcYrn3cNPUPd1FyXRqPFozuOIZxcMMbwo33Fz/gzBM+a50N/2eGOx08xX63zT++5iXzOodnyKHX1UqvM8NjP/oMbXvZ/KPctDooOaRngksHjyHAmFpD2ZTJa"
    "TKl+5xyWgENCKj9FZA65bDGThgJCRDpDV0h2MzOryTwC0sT3tD9JZ76E7nS4mQWEwXFLNb06Y0Ghs4svMwI0lnKqYRO6g6ll8DPWBz/0wQ+ZFt1tHIKM3bPI"
    "Sk2zI4Xshe5k62r6MkQHRoa4kzhBJuhLx/cx+RDajBJOxjXPBK9xAZgrNQ/U7UTCBSUtZBXBnWd1aY3+AqMcUyceuiMq5fHYz/+LiVNP0z+8BCmDMUXeEfzT"
    "157ihw8eZ7Anj+ebZVxCJtIRzKvTUKfZcqVs5XXaN6BjUJBo06PFcLJtW0xXmgz0Fvi7t93Iu15zBYt68+zYf44PfPYhfnjvYXJ5m7wjg88sTBWPSEGHbfPS"
    "VGkWBbvJhC0fGVCbpC2S1xck46kEeTAe6szITIQwUrNW4+Jrb+S2N/wuw0tXMHL8GNse/DWHnt5Fq1bBsSwsKcOxafA6vtvEbzYTYbdhhS7MNacTbbtWKigc"
    "Ug6LEtvOIW2HVr1OodxDV/8A42eO06zOhw6eMjyALSzHoVGvs+Hq61mx6WJ23PsrcrYMc0qC7t+SFtK2g3Rez4/lrL7ncdHFW1m0ciXHdu9g7MxppJT0LlrM"
    "qvWbAw7HqePBdw2h+Vy5i3yhRKtRo16ZD1xM0bghx2LpmouCnJd6Hct2Eg+ScFMORjo+qzdvxc7l2bvtYULr2HjcKCJuUHgtit29uM1WxhU5kdqKmJCqyeXz"
    "OIUyvufiuT7StnFCefHE6CjnTp2kUWtQ7u1j2dr1LFu9hpnxcWamJnByOWOjbedYmZRoISyS6i8h9LVDyaLtEGkzJ+y472ijidFh4RX45pSLOc6Mz3HHg0do"
    "tly2XNTPljWLeNaVK5ieabDr6CSWLclZVlLQR8R6HRB3LUtybGSKH97xJPWGy/R8jWajQblkk7NtWq7L+GyNP/m9F3Po6Bn2HDmPpyS7D58jn89jSYll21iW"
    "jMfA8ZiLpEhQStNdsjl8eo59R8a57dpVFPIOLTdwJW3V5zh7dCeLV28lX+wORoehQZ15cIqFbAREe5iYWEiFYoxmkj2h837cxh9Bdz4HF7p/QixERkkpUtq4"
    "QR2+hykj1nTmLkIa7e2oKBEX/vxmASMyv/dMKqDA2rxN+hmST8z3jOXBOoWISLMOy3y5bEUekzkXqEC1yfeQsiPDOM1nEW2DLZElgGYqw6xOWSyUvped65PY"
    "G4vM90GI9gUdEWilbNM5x4SxC/hr6GwXJEVInJIo3+XxX3yGiZEDDAwtQQiBY0ksS/LB/9nG7dvOMNhXQPkqFeoTsf8JQ6NarpsQsWIIK3GLFLr9oNdGBWzK"
    "u2NyKalgVWwpUUozU23ywutX85F3P4ebL11GrenypZ/v5oNfeIjT4/P0dufjQKtIDZX4FCQbdfqhN6p/kQ4eFLoD+UknnVwqlyglZet0703YNjj4W/Uaz3rZ"
    "q7jxJa/G9332bHuMXdsep1aZJWcF8IXvuYEtudH9KM/FrVdRITnTioiW4TOnhY47Hh0iN/muEoVSCd/1kgPLgNeV7yNsm1J3L7Nj56jNTWNZdkyctByHZr3O"
    "RZdcxuXPei6P3fFz5iZGg6IsRBW08gMSr+OAIvTFCIohz/dZd8mlLF21iiM7n2T0zGm0gP7hxazcsInZ6dDaPEZSJMWePoSU1Odm8dxm8H1CHXNldopyzwCL"
    "V65hbnoKr9kKxhW+H0FRMQa6dN0mGrUaI4f3h6RZ04NHBAe61vQML2XZuo3MTk3itZqhb5mKvUSSYCCN9j3sfBHLsmk2G/H4CBXwZZyw6JocH2VybBQn5zC8"
    "bCUbLr6ERq3G+dMngqJD6UQFZUhnZUqDKGMTmEAeK1IAR1T4mg2GSGbTxh6zgIRTZDJbhBmkBvm8hbQkDzx5iu37zrN8cQ+b1wxyy1XLWNJfZOehcWYrTUpF"
    "O+DeaG3ox4L3KeYcWp7P+YlZHFvQatT50J++gQNHRpiv1Tk7OskVm1dx3RXr+d4dTyJtOywyLCq1JrOVBrVaC8/3yTtWcFooDSnNYKD26irYnDxf4alDEzzn"
    "yiV0l/I0Wy65XJFWo8KZo7tZsuYy8qF1vsicU5qFbSXaTNY6ekykz8KUE7ToDEVnrQzMIlRnmhWZOjMWUL9kQt7MULaECydTZMzYPCz8OdmhWM16TaUngmnP"
    "E50xm9QLXKdU4GHG2LFToKrsNEpYOKE1bblqkk86kkw7OH2mbrDpUJqaaYnUIZBCSjqgASnEwFSgaMOYBCOILTNW0VlmcEbLLLIKGlNKi74A8YYUj4MMeUjQ"
    "bvudVYwQKlACGDeA1h/7eTBG6e1fFCRv2hJpSz74P09w15NnA0Ov0KaclMQ5cIXUaObnq6xc1E0hbwcKRiNkKmbCp7oH4tRWESIk2ni4dNS1RJyDcIRSbXoo"
    "AX/zBzfxsT96Dhct6ebpYxP86b/fzb99cxsIQVcxkLqq1KI3Rzg6FQqkY/8bkUaDjCGlNqyCTYt2FImLaoaTk/bsSpwndaYAdZsNnv/G3+P6F7+S6vwcj917"
    "Nwd27ySfc7CFpNVo4rnNoEtE03Jd3GYTrXxsJ0epty/gWWg/cOMM/VOQxDPh6FYUe3opdvUiLSdAArQJjfr4nodWPvPT40yNnQs5IQ6EhYzlODRqNVZtupjr"
    "XvhStt9zJ2OnAs+MoOgJ0A2tFJ7bCmynrCAKPk5IEBLLsnDsMKXUUKdFM+bY80OAtHLYTi4w9fJVuPklckMp4NSBXTSq86zefCm5Qh7leUZiZXLPle9TnZ+L"
    "C6IUMS+RagXfA4vuwaHwfkkDqtb4botmvYrvNrFzOUrdPViWhe+28FtN/FYrJOb6uG4DrRSObdGozrP/yW0c3bcbLItbX/kaLrvhZhq1elAsItLnZmyHHqC2"
    "WvuhRJiEiGxY4Zv5UO2yhyjnQyVquRglyKAiYcaP0qZSL3bIp7+vyJ4jE/zRP93N53/4FJ7yeOMLN/DZ99/GjZcsZmKmFnqeCIPYHjwjvh/4a9gyGHvlbIs1"
    "K5YghaRaq6OadR7avpfNa5ZSLpXAcvB8Rb1e45Yr1/G373gRH3rfi3jBjRup1xs06g2EUPF9iq+FVni+prc7z9Mnp/nz/3iEmbk65WIO3/colrrxW7M8/LP/"
    "ojo3iZQWKkK9hOBCzlViAdQjJUnWusOoXCfu1h1HGTodXZAZ24gO5O1sI5zKixGiI20hyw9M0PyAAxXIrkUbLSG2uDDOgd/AVzQ1YbnQhMCI00pdn4XMQ0OE"
    "4zf5CNEXXTjkxbzwKVnrQrHsHS7wQgd+Aod3Tt1LyJB0HM1kxyYLRbxn7WhFylGuQxWsRQaCegYoaqGbp1mggk48T9CaR37+acZP76F3cDECyNsWlmXxgS8+"
    "wd1PngndQ1Vq5zIRL0sKatUmz7txAx943xv47s8eQwuJbVnxgpYi8RmRGef8lPFMxi48+jMZBq5NzbW4aHkvH//j5/Ky61fjej4/vP8Qf//ZBzh8epq+7kLE"
    "xo3Z0OlQPZFCGNKKpPa8nNT4I1Ws6nRarczWwh1mjpFRWQZqbdUb3PaG3+aq576A6bFRHrrrTibPjdDT24u0JM1aJQxT07HxVN+iJSxbt5nhVWvpX7qMoeUr"
    "GVy+ir6hJeSKBVqNFp7rYll20r0QjCW6+obQCrxWk1YcGx8myEZyT88Nop+dHK1GhWa9ipQO0nZw3RZL12zgea9/E4/d+UuO7H6SYiEPkbeBMDgOWmPnCwgp"
    "cZtNVGhp7nkeGy7dyvI1azn41JOcO30KIQV9w4tZuXEL8zMzgdNo2NUKyyZfLuO7Ls16NVS/RDLX8B76HrOTE/QOLaJncIi5iXF8t5VWJApBz0BQQMyOj6YJ"
    "l+FYKXjuJc1ale6+Aco9fVRmp/DdJlIGSptWo065r59VF1/O2suvZfmWrQyvXMvgspUML1tFqacXz/doVOYC/w6RpGZblkW+VGZ6YoJWs8nQ4iWs2Xgx1bnZ"
    "GOlQShnoVEbeKmS4jlRa0Rcr1oKfiVVEMa1Dt/PfjIBN0Yn0pWl7KqK16/uQz9kIofn1k6c5cHySDav62XLRELdcvhJHwpMHxvA1FHIWKts06QT9aLZcnj54"
    "it95+Q2cHZ8in7P5yF/+Dtv3n+J/79mDlcvTVbD4wof/gD976wuZnK1wdmya9SsHee516zl0dITpuVoQsGgmZoX7rO8LynmLs9N1Hts7yrMuX0ZvV4Fm0yVf"
    "KNCszTJybA/LLrqcXL4UFh3Z79xOwOx4lnXo3lPdvu48KlhovGHKaUUnReZCdud0QBtIm0emTMwyAWrZEUkb6tJBgisupDDNpLW3CSWyr9eh2Oj0XdtGKtlf"
    "poNSKPbkJx39RiZBruO8yjRUSRWZeuECJBXE1XmGpTMR97pD8ZJiJC+ovc4eVmKBCOMO18hEOmWHgKEOJjKJi16H+kMbmR8SHvnZpzl/9Cn6BoZBQ84JNtwP"
    "fvEJ7tlxjoGefBDARnvKZZzsqsG2BPOVGtPVFq99ybO479Fd2HbkOZAocyI7XTP7RBuy3HRxF3RjQa6HYqbS4hW3rOOj776FTcv7ODlR4V++/jif+9EukIJS"
    "wUZ5mqypyUIZB2QZ1h3QIEEmejs2rRKZNZpJ1eyQ2puNupGWRaNa4drnv5hrX/wKKlOT/Pr2O5idmqBYKpHLF9DKx23UAY3XatHVP8RFl13D0rWbkJZFdXaa"
    "uYlRqlOTNGs1csUuhlasZXD5KnyvRW1uBstywjGXpNw7gFMoon2XRrUSEkeTzivuQpQXEiEDEqQMx1iBa2mJl7z5rRzbu4cn77mDUrmIH5L4hJSxEV1U0Be6"
    "uhFCBJLd8DV9z2PD1q2sXLuWA9u3cfbUKYQQwUhl42ZmpycZPXUCx7YQlhVwJAoFlFa0arVY6hotF60DnwzPbVGrzDG0ZAV2zmFucjx0OQUpA06F57k0KvM0"
    "a7Uknj6SmofjSikkvhsUYz0DQ3huk9rcdFAsS4st1z+by579QnqHllCfnWXy7Agz589RmZ0FYdHVN8jwitWUu8pUpmdw3Ra248TcGst2KJTKjJ87i+s2GVqy"
    "jJUXrefc6ZPMTU0EaE7HRsjgMZG2QQ9vQKy2Etm9FNPhN3mGdcfGLLGux8zHMMaI5vZWKtgcPTXN3Y+fotxVYNNFg1x/yXIuXtPP7sNjjM00gudTpa3mI35I"
    "3rEZGZ3ixNlxfvvVz+bNr76Vw6fG+MB//RRl5fFcj6//09u4fut6XvFHn+T/ffNuHt5xiHsf2sfsXI13//ZzefCJI7R8Hyss7GJEhaQGLuZtzk/XeXTPKM+5"
    "ail9XQ6NRotCvkizMsn5U/tZtu4qbCcfj5sXoh50PAQ7jLUvWBBcMMkt8qaQ6ZRps1kmbf6VlZ+2cQYz/AiTHJxYupi8Dd3GQ2kfJaVDPsUCQadm9oxYKMqk"
    "g/pCIDryZRZEOIQ5rzcS4tKMWpGVKZMBrhODrk6ciIWIO1nXN6MqbCPnLJD42ukm0ynCfgHkY0EkJhPIZfphtA8MddoVNMP90AuiHdkOKNhhpGXx2C/+mzOH"
    "ttE3sBgpwLEkjm3zgS/u4J4dZxjoyeN7UWCQTsXVRwodSyZITa3psn3nIRYP9fM7r3o2d9y/nVzosJiKhzZd64SIfSJoy5AggKDDEcpf/u7VvO/1V9BTzvHo"
    "vjH++r/u4/E9Z+nrKYQSQFOGJY2sBwyiVkpo3+ZFIIzcD2F4IKQUJhE/xSgkhSFnTjoInZ6VG7uCkBK3XmPZuk08942/h9dyeeTOOxg/e5ZiIY+dy+HkCyil"
    "8dwWXqPO4lVrWXPZ1SjX4/ShvYwc2c/U2VPMTowyOznG3MQoU+Pnqc7NUejqYdlFG4Ik15kpLCcYn3T19WNbNo1qhVajbnBTtPGkBV/Y98JodjuPnctjOQ71"
    "uWmuf96L6RkY5J7vfwuBH/NzzFFUFPqUKxTpHVyE57u49Tq+CjZxz/PYsPUy1mxYz/7tj3PmxEmklPQvWszKDVuYn57m/Imj2LYV5Lq4LYSEXKFAs1Y1FDbG"
    "ph8mwDbrVbTS9C9ZQb0aFBZWLo8lrTBbx6bVbOC2WomxGJHMVyRGViHhtGtgCGnbzE8GHJUbX/VbrNqylaO7dvD0o/dz5tA+pkZHmJ8aZ2biPBNnTzF++gTz"
    "E+OUe/voX7yY6uwMbqOJkwvug9A6GCnl8pw/cxqBZtHylQyvWMHRvU+jfT+2kE/bQXWil7cT87RJvmszDjMOnPB3UvN5nVYapJ+ddA5JtJ6VgmLeoun6/OrR"
    "44xNVbl0/TBb1y/i2Vcs48zoPHtPTFPMy/jwl6FKS0qJ0opCzmFypsaDTxzizgf38eN7d2Hn81RrTV7x7C38zTtfxhv+5DM8+MRBhgfKFByL7u4ih4+M0N3T"
    "zVtffQO/vG83hUIu5U0UfXIpBb6vKeYtxmZrPLFvlNuuWkZXqUCz1SJfKFKdHWPi3FGWr786kMpiKs5Ex1HKhdCFaC8wVRYLejBl9nydGbXTKScshcCaYoM0"
    "709ks09Mg8lO/McFeBoL6SPEQudP1qcq5cwsUmMm0QENeSZOTNjEig4sDSPIxrwAusMnzhDChUFuSXEUspLS7FxJiAX1y3ABs6oOv5OCyDqSXNLbQtv87gKj"
    "mLRsN0MmykKfHZp0nRkT0ZZSab54AGlvu/NLnNr/KL19w0G3JC0cx+Ifv7qDe3ecYrAnh+f5BkKgU8ZmGsHMXI3J6QqTMxXmKnVaLZe+/l6+9eN7OXjkFP/w"
    "52+kVqsHRkVpMktG6ixSHIcIbrVti6lKg6WDBT7zl7fx5hduAu3zjV/t44//9VeMjM3T350Pxj1tpNykgFMpQqgRsR55NQAiPnhCG+CIU6IzhV5cTKiUDl2L"
    "hIVvljXJvNOsvINBuHQcbn7FG7DzRfZue4Qzhw6Qz9m4jQaWDNQeIlSlLF+3iWUbLub8iaPse/wBxk8fw2s1sCxJLp8nly/g5Is4jk29Msup/bsZP3OK1Vsu"
    "Y+XGLUgE+WKJXKEUuoPWycZJi1DFE90bGR3MzUYwMxI2wyvWsGHrlex48D6qc1OhLFyl751IOAU9A4vIl7vQStNqtWK+gK88lNZYtm2QiAXCsnFsJ+gsNdjh"
    "4ex7Lo3KLFopij294WEs0xkPMSJmMTl6hmatSv+iZSFJLsheUcrH84JxkwjHTCLMwtG6nVnveS2kEBTLPfQvWsYVt72EwaUrePiXP2Hv9odpNes4+Ty5Qgkn"
    "n8e2HSwp0G6T2bERDj21jZEjByh397JiwyZsJxdLuwObepec47D/qSc4feIIA0tXcuUtz6XVbMUIn6lj01mYPbXaEoedNmwtKiykHV6n9CGnMQj8Ip21ZOSb"
    "phCQOEMlvFauHxR8/d05fnjfAd77z7ez89A5Vi/t4V/eewPvesUmKjU3MD6TyYEXqbSUFhSLBcrlMtqyGOjrIScl2m1y23UbeWLXUe57fB+9fUWazRae0rR8"
    "Tb6rxJnxOYaGBrGsJDMkxovD91AqCAdzPU1PMcfRs3P8+aceodpsUigEY6xSVx/TZ4/wxB3/DcrvnND6TGyBbHduNFELFhvQ0dgqGT+ZyluDQ2ciH2a4aAdl"
    "SpafaO7HKTQsW4RkRkPJ++v0calDVFp3Ri/a+JMLiDEXHOFkCLexMrLdXKP9sNedCDIdf0ebzq2pSk+b8fUG+7ojycU4nDVpt9FOKXnCeP0LkTGzBYbuZPq1"
    "kGIlC5YKAR2ISu2TF935swjzhrTfSO0rpGWz+4HvcnLPvQwOL0LKAEUoF2w+/u09/GrbmUD66hnmKhFDWGu09uMZ/x+86lo+8O4X8PbX3sBt125kw6ql9HaX"
    "WLZ0iC9+706e2nWQP/ytW5menjWMetJpqsli1YaBWtD9TMxWueXypXzu/S/g5ouXMD5V5R+/+Cj//OUnkJZFMW8HUD6m7Xn22RNpONJgX0ftXZDzouMuRptT"
    "zA7wYIKEBddGmXk8QqcC9BDtqUqB22Wdy571PFZuvpixE0fZv/1x8vkcbqMGYUKmVgq/1WBg6Qr6l6/mxL7dnD74NFqF3AytYhdPAGnbSNshVyiSK5SYGDnD"
    "/Mw0ay65iqHlqygUi5S6ugKuRkgqNbvgpFuS8XeXQuA2qvh+C+W5rN1yKfVajeNP7yaXy6cJzGYMe2hB3jM0jPI95qcnUSpRNCWFpdGMSIkUVsD9ERorRAM8"
    "P0BavFaTZr2Oky9R6u2PPTwEiTVzVFD6bovZifMUunrIF4v4rhvzlryWi9J+OKI0H8coTVqGajALESIytu2wYtOlrNy4hd0PP8DY6VOUu3qxpB3D0MoPcl7w"
    "/WAMFfJP5qYm0UKy/qobWHfZlSg/LHzcJsr34syX/U9sY35qkg2XX8XilStxm820Ii0KaEzJu9INnTaVMzEBNyGLxjuhgVbGz5/OUhDDrCZM+N60X0j2q8ig"
    "SWmN52kGuoscG5njff9yJ/977wHyjsUfv/5SPvbumyjmHSoND9uWxuuE+Tk6IXYrL0qKVZQKNvsOncVrucE1DmXqUggsaVOvN/jY534OYYFu2QH6muLfGv1N"
    "VHTsPzXL337mUXzPJxeqXbp6+hg9vpsdd30xNp4jlcJ64bMtm9qts82foKOypV1RKJLxs0iTKdPzsY4v3P7ZOiWdm/c0Olc6jE7a3lZ0+kCZTTiV0ZLmDy0Y"
    "0po5z01wopMKSAiBrXVn+myiIslUUwvNuXQ7ezd2QTMDsoRIXazsBc/yHjoadWV4FdlZlF4gulhnvDSEEcLWacans7kuC0X4Zg/kFI8gTYDVGeWD6QcRSeGV"
    "CjIfDj91B0ef+iUDw0sQWiMdi76uPJ/47h5+9sjJ0GdDxTiVOQqIunfX9VizrI+brlzPa19wNXsOn6KvpwdbWvEcWUs4euI0V1++iVVL+/jQp35CoRi6h0ZB"
    "XQTzWzPoLyDlaWarDd7xyst4z+uupCsn2XZwlI9++XH2Hp+mPxz1+BnlTXiSGrbuEZQpUrIqbaBIsdNmnC5q3DNpiKh0plzWocw2nn2K+N/TlGxtrAFilUT/"
    "0CKuf8FLEMplz7bH8H0vOGiVDxKU7+E1a5R7B7jk5mdxaMcTjJ4+HmRJhEqQ4EEW8eEopRX4XghJvlDAcnLMTYyyZMVqlq3fxPTZM5S7umlU5snlC7huC3CD"
    "QkClGfZam7Cnwq1XEcJiyarVjBw/QnVummK5bBgdZdj0CkrdfQhh4TYbeK1WeHhGyFFyTWXMKwhSZaUM9CBOLiCbRk6cyld4zTq2kw/f00/IwaaLZsjBqM5M"
    "Mrh8DcVSmcb8LHYuHxQcoTuuiJE2EjKmkAgrKGIsK48QGktKyt09DCxexPT4GOfPjtDd24tbr4aKEZUET8YuyAqFRmpw8gWmzp/hzOGDrFi/kZ7BE0ydH8HJ"
    "5+Kiy7JzzE5PcnzfHi678dlcdsOzuOeH3+ugzNMJIk2bUWWaYBdBzrYTkCBFNCaKjBUNeCPtA0hHC0Ozy01wSuKwOYO+5HqaUt6m6fr83/9+iP3HJnn367fy"
    "khvWsGZZL//4xcd5+vgkgz3FYL8Jr38UBibioin4b3sOnOKai9eim00sWcALfdKU0uTzNgeOnkVI6CrmqNQbNN1AgdRVLmJbAj9SNhmdeWAOlmPH4Wn+7nOP"
    "88/vuRbHkrjap7t3kDMHH8Mu9nDFc34nML4LCdga2nlzFyBpPrNoAhDZGLAE0RdCpkLbzI6/YxykGfDWQcjQbtsQOW/reOSts4rIZxi7aDqobQx376Q+1h0p"
    "DSZFQWdfV4uUoifb8dumHCsVK9z2AXQbXyM9U6IjqTMrKezkVWEWCmnPBNEOkbGAe2mGtJUy/jJDoTrIaoXuHGKTNrky38dIy6ODoqLDmCWlu0/jeIFMU0fz"
    "VQ/Lcji5/2H2Pvx9uvuGkCLQqA/15Pn8zw/z7fuOMdSTx/OMzxsVTuGmH8F5tm1z8vwMb/uLz/LQm59Py9f88I6H2LB+Db3d3QwNdLOor8yS4R4Onpzk4g0r"
    "uP6q9Tz21IlAjqYiYx6Viue2wiwUITQfeMfNvP62zWjh86MHD/Oxrz1Jo+EyECa7mhue1mloMUbDRLLr6ognk7WyjyFkaSQhEks3VeTkl0kui8PB4oukDcxZ"
    "xw+XzhC5pLRo1JpsufUmlqxaydPbtnHu9CmKxSLKC1wTfd/HbdVxm03WX3Edtfl5Tu7dg21Z8UjHLH9VOBKTUiIsK4T4C+RLZXK5HF6rweCSJUFHKAWFrm5K"
    "zf6Ax1FvBORIdOIOqZOOSoQHlNdoIG2bnoFBThx4OizQtFFTpQ3QhGVhWTae20IICydXiIsOExaVmbpbSBEbjIlQshv4XgT+H8r3Ub5LY342vl4BqiTiMEBC"
    "+3S3GdicF7t7mDo/kt5gozpXp0nLQgajJNu2yReK5PIBn6ZrcIjegX7279pFsVzEbzTwWxbKEylz7YC7EppzRTwQFaALJ/c9Re/gIANLlzN++kTYpftIaSOE"
    "xMmXOH3iOKs3X8JFmy9m78pVnD99MsgOCW3XU52n1mZObPL/M0ZgkW27EFZsIa/DOASdKS5iT4+osxdpErXOGCqmQ/2SzyGExvfBkpKeco6v3b6XIyMz/N3v"
    "X8/Gi/r41F/cyj9/dRt3P3Gavp4ivq+MuIFkT/Q9j55ygW/+4jFuvmo9m9Yv5eCJc/T1dwXuoEKiUOTzkmbLY7ba4Kotq7lkw0qm5qo8suMoM5U6PeVCaLsu"
    "YnVPoF6B/u4CD+0d5x+/tJ1/ePvVeL4EoSh293P0qTsplPrYfO1L8b0W0nLSB6ZYqDZrJ/ibe7lOyfJ1Z4pE27XO5IBFszkD49IGYXIhR26hE+dOodMHvGk7"
    "rukcaLogcXYBGax5RumsajN1PhuO0uYoyci6av99sFOkFLPSaTMkyWjFM/wFbe4IWqTlAs9E9M1WUZkiQz/DKC7IY8heEN3R4lYYBYg24JfUojQWXmSHnlqI"
    "Js04Q/1IXZMOxVK28tNm/oMfFBujJ/ew676vU+7qC5UEmsHeAl+/+xif//k+BrpzhoNocL214bkhrchwLLhnlq8YWtTLF797F3/97jfxhpc+hy//4D66B/up"
    "1Zr4rgueAsumkHfo6ipTKOYDKDvOf0iWpWVbzNWaLOrL8/fvuJnnXrGCeqPBZ3+0ky/+5AD5kk0pb4cbU9QNpaV8cUB2xMVoy3ch5c1vyoMTEmICfypfpVja"
    "KZtfkZBJzXGmNkY4kdOi6QqrtCZfLrPl2utxW00O7NoZ+FBEx7cORiT1+Xl6BgYZWLKYvY8/QnV2ikKpKzg8iJCZIGrdCh1QtQysxi0nh+PkKXd30zs0TLG3"
    "n9UrFrNp/UrGxqZR0gbLRoyPofVMkF+iNOAZcmkRJ6xqNMprYed7kbkcXmjuJoSMfU7CpJJ4U5DSwnVbzE5NBMZXxQKe22rzzZHSQAjj4kIgrQCx8f1WbKKl"
    "IShiWk28ViNFdBWIcG2FD00oXy0Ui+jBYUaOHExI31FGg1GoJp9HYts2uXyRcm8v0s5T6u5m08Y1uJ5LrdmiVOqi7vm40goQOR9SqZYicacNsld8LGnTmJ/j"
    "zKH9LLloA/lSkXplDjtfQGuwnRy2naPZaDBy8jiLr7uWNZsvZuT4sUAmazZlZsy8WTyZ8aEGbyg4mBNHTjMvKEEXSY/XBKDCxtDILjJRvMgVWKSaIyNuIRzn"
    "+1oz2Ftk297z/Nkn7uPv3nEdV29Zzoff/SyWDz3F1+84RFe5EFrE6yRTJPgDHKGZm6vz4U//iPf8zq187aePcODQCKXuIk3PQwqo1losGe7jk3/9e9xwxTp2"
    "7D/JyMQcr3jeVXzrJ4/wwPbD9HYX8ZSKc6cizpLrawa7c/zqyVF6S7v4qzdfyUwVLCEod/ex99EfUujqZc2Wm1NIR1oQIjrswwuM6VNIgKEoylQIFzqjdGQw"
    "kB3f6LSPURvanxp/JkiwiqgN0YhLtxeicQ6Zodxrc0E1kcZUnIc2Rvxi4RGSSdJvU/qYhNJEnWanxFiZoiNOOxXtFzitkzSOEJ0mQXasji4gR41IeqmqTUcm"
    "Ojrl+KnNXJUFIEWhO8ONnYqqtLlL5t8XyD7XJttcZQqWbIfTwQgtXgzaQ1o2s5Nn2H7H58nlcli2he8rhnoK/PChU3zqB3vpKeVCR0DT+TNUs4Qfca7SwPdU"
    "aMSkKTo2RcdioL+HT3zxx/zt+36L33/9c/naTx5iqL+bZssK4ralBBnI4URclAcbnwxlptKymJ6vs3XdAB98+41sXNHLuYkZPva17dz+2Bn6ewqBiZRKNiHT"
    "WCc1VjKg8tSDFkvMQstxGXThylVIYYVGN9p4JHVolyVSRUYyNos231TyhFHFGzH10f2WArdRZ/Xmi1m6ZjVnjx9jdGQkMKly3YDUpgLCqtuoM7ziCpTvMnri"
    "WGjVrQwik07LG4VESjsoBCyJk3fo7utjePlybrtxK1tXD1CwJLWm4n/vL/PEkz5es4kbqjUQLYh5tOH831dBfkxYfBEe/EpphGUjpGVaLCWFvdJI28JzXXzf"
    "jQ24pG2HYzMZJ9dKkSCRKhpN6MCrQghotVoJ34PAdCxSqUSSyijLIdWNSYnyNflSKbFwz0DqGh1kzqBBW3FukLSsoFCRDluv2MofvOU19HYVcD2Pck7wvz+4"
    "Hduxg2Ip5HsE4XgiyUHRAoUf5ogIfO2htWZi5DhDy1dR6uqlMjNFwCFNSHiWZXH+9ElqF29h5UXrKJSKAXlYGoocM6DCIPxhjBKDZ0Glq/FwDCeETLlVpuI+"
    "NKmO1SxuE5JncN2jXJ7IVC22a4vQGGMTdD3o7SpwZqLGH//r/fzlW67h5bds4L1vuIKBngKf/uHTODmHnC3xvGAUFTWgnlaUCzn2HTvLJ792J296xc38/uue"
    "x12P7uH2B3aSdyy6SwW+94k/or+ni9f9yX/y+NOnEZZk2WA3f/UHL0drzUM7j9FTLsaOt6a/tevBQE+e/33oDP3dBd7+qi3MzHtIS1Iqldh9/9co9wwyvHxz"
    "nLsijPvR5u91ocZQLNAc64U9OrIUgHjfEYkaSWmdSZvNnEeasJBLq/dTKeLaiAo0GjBlmGiatlBqAfQjiVJoD8ETGSpDalyUkcHSAUfLghYylZiZMhYRbZ1m"
    "m9GMGWcvOsuQtMnXMJCHhQg4ooMfR+q1pWxngGccS1mgehVZgo7J4hUd2LkmkVjIzuTRVFXZmaBKpmuP/lXG1V+wFBq1WR7/+acR+OQLAXQ50FPg3qfO8y/f"
    "3EVXIUir9JU2NPk6tGT2cX2P+co8z712I//216/jmx//fT7yf17NhjWLma02ETJHT283//y5H7Nh1RJe87yrGT03hhQCzw9gVe37aO2htG+QO4N7atmSmfk6"
    "L7pmGZ/8k1vZuLyXw6en+D+feIBfPX6GgZ5C+PsGJyJEN3SbjIgwWlt1fMjNDAOtNH4IySfuoBnH0TY6kOlMqlNdRDYrRXfIBZAESbxrNm4mly9yfP8+3Hot"
    "KIZ83yA3aywnx/DyVcyMjlKfn40dEHWMheiQ7yATHoAIpH+WZZEr5Mh3dbNlwyqu3bCEvCURWrC4t8ALrttI/0Af+UIpkGiGxmoixc5WIfScPAtKBR4HtmUj"
    "LAcRjgKSUUp0qPlYloXyXfwQjXCb9XicFfhiBCm/VnhoB9/cB98NigghaTVqwSYa2nhLK4D5fd+NCZHJnN9QUQmJZeUQUtLTP0BXT19YoARXTsXPt+n5Ej47"
    "UmDZDpaTo9TVxVve9ArWLO6nYAlKtuAVt13L5os30HI1juMgLWlEsRMSWaNJTxg2qBTKdwGf+vws9coc+ZD/EqNIWqG1j5QwOznB+Llz9PQP0D+8CN8N7LaF"
    "prNOLXMuqbi5k3EBImJCl2hXD+ownTQl+8wEh8UNYnj9gv4aKXRM3BUp9FDEdP/oDHB9RSFvo4XkH7/4OJ/93lM0PZfffskmPvrOaylYmkbLx7ZlGlEBPK3p"
    "KhUZm6ry71/+FZ/8+u3ct+0ApWKBynyTv/j9F7Nx3VJe+b7/4rGdJxjs72Kwt4vR6Sr/7zv38J43v4CuYgFPp3mD5ibie4q+rjxfvOMo37nzEP3dAZKRc3LY"
    "ls32X32e+elzSMtOxk3xb6tnlMgupEqJOEpZjypztN1J1JBwcnTbuD6lhonPPhNtNdCyzLjeDK+7kOXEQlbvqbyWrENoB1FFRE6PT0PZGR1KrV1t5He1H50p"
    "zWcbs/o3UBctjDZ00v92tPbWaSTiN1gYGjqjFqSildJdbEentMzrRVdnIUJiR+pzh2IjFYxj/HBkiqQVj/7009TnxiiUyiil6O8usO/4DB/+yg7yORH7VwSj"
    "iFhzkUgXm3X++M0v5JpL1/LYnpPs2HuSF9y0hV998a9535ufR62psJ0CPb09fOQLP+NFt1zBZz7wNnJWmJwalTCxUZOBHEjJ9HyDt75kAx9+57NY3Jvnsb1n"
    "ee+/PsD+k7MBOdT34wcuPizCGXFSDetUuJBZhFm5XMh+jy9Om6WwaVttSsbMAsAsRqO0TiEWGGl1ehClxG3VUa7L8vWbmZ+dZeTkCXL5HFg2VqFArlDEzuVR"
    "WmM7NvlSN9NjY/iea9xq0VHdpLQKD2qBZYVqlVKJxQM9WFEnYEnqrke5mKdYLMSETSskS8bXx4Bcg5FK4NuCBgsolruDYkNacbEej0RDFEZaEt9t0WrU8VwX"
    "5SkjUCyUIUVGVSHvQauAR6IReL5PqxkRTUPVjLQQwkLauTBpWKWKfm3YegtLYjt5Fi1bRk9fd/i5gqIvCtYzyD9JARyOpJS06B/op6e7TLXWYKA7kBz7vs/K"
    "ZYsCtMVyAq5GZIIXLkIpg9wPrVSCFoVKIu17NGs1nHwxIJxKi1yxiFMoBjwcwGs1GB87j5Y2Q0uWUqvMB9cgvE4RN85UxIn2HjhZ41IY0nRDnReFEQpSkndS"
    "TQExuoTOFt9RvooK6zeRyGsN+NzcrnxfI4Wku5jnf362l49+6THmqg1ecN1q/vPPb2VJb4FKzQ2LDhXDL8FnDcjQ3V0lzk9UsKSF0oK+/m5e/6Jr+cT/3MHB"
    "42MML+4PSOWeT193idNnx+kul7n12o3Uaq0gE6hNth7x3QTdpRyf+tEh7tx2KvAi0pp8oYzfqvH4L/+bZm0+HCeqdOaNQREQC3pWiLRUfgFuQBsvz/isgiSl"
    "OpseYyKsHQPc2g4iE6HCiLVP99kyo1bRxnluqnCU0p1q4NQ1WggYIKsq7ZAJIzocjXYn7kI2HKidjpHwNXTWqCtcrUk3ZyyQTjKbjOJE6XQ8rzbRiywykiWg"
    "ZoiuMZEnnoXpWK0SISkmKTHJK5Dhe6sMpzgTlpOVBBuVppZGjHeKJCmi8zJUHCgs2+bx2z/P9Mh+eoaW4CtNTynH2Yka7//CE7hKUczJUFaaga+UQgpNda7K"
    "H/3uCzl8cpSf/PxhZHcZjeRz3/s1f/Pu1/KxP/9t5mpNvn77LgZ7S/iVBvc8vIvpuXmmKk3KhTxKkPjyhxW5DPkxlUqL971uK7//4ouxbMEvHjvOh7+0A1dB"
    "V8kJHE5FOnBPmE6HOqHKaZ2F6qIDUyVyz2gOGYafBUB4sIIsJ4fvtsKDQ7UF3aWMyhAhmTQK2kqSYQNn1UR2qEOYvlmt0N0/wC2vej0Dy1YxNTZKdS5QiwjL"
    "Qji5gLcgAuMq6bXA96jNTgd+AEoZPA8jXyZ0cpXhOEL5Pn6ICAlfUam1yNkWntb4SuPkJGNzDWZn5vGadXyvhVIehPkcWokUjyPqiKTt0Gw1mZuaYNmaNYZh"
    "VvhchugLWiGdoMhTrTBoTpicHeJDUMpgHUTrgjBgT2mNH1q4m5h/NEiznTy+20L5rWCEoRN0QxrmXYVSiaElSxg7fSImKEdFQHs+qo7RMd/3sYTF7GyFuUqN"
    "oWWDzNddWl6AKp0eGUUIjdIqgdMjgy0NVi4ffKdWA6FI+RZodKBIkkXK/UOU+hYFxlduC98NsnFQPvPTU3i+z5arr8cSgoN7nsZtNnHyTmCVbnAoiKLBw2dD"
    "GgTSAJlK6ryktCZWaaXm9eG4RBhGdzL0hAkJaPE+pmQQHifT8+J4DzejA2Jzv7BY8jX09xa4/dHTzM41+ft33MCl6wb45J8+i7/73KPsPz5Jb5eD50VjAwky"
    "bCyUoJALyJtNX7NsuBew+OkDeyn0dgd7WqiyU0qRz1ssGijR210M+EDRWowKJZ2Mt5XWAXej6PAv336aFYu72bhqiEq1Samrm/npszx51/9ww8v/T7gnp49B"
    "pTsqUBeWwRqEXNOhF9P1V6QPcW1SFToc6pEnkEkJFFkdRFz8m8GGRkEUIbQhuqiVajPujP1dIk6RjgzIUtmxBgldJ3meHYy/soWyzDSRqlMWGqGgUvAMDma6"
    "/ZFvMwHJZIhE3ZMwYubNTJW2es5YTdqoLAM+lOjsD2LCbdEBFc/YsgQZ4wMbcyiVcSeN2duathpNpOay7YsyWwwthH4kI7ug2Djw+E84s+/X9A4uQiufgmNR"
    "bbj87eeeYHK2STFvxdKymIAUbkZSCuotl83rV3DpJRv4yd3bGVg2QH9XkUV9XdhOnr/75Pf4+s8e4a/e+nx6SjlqTY+eriJ3bzvIr3ccp1wuoUUAnUes6OBQ"
    "1nhK0Wq1+Nvfu5Lff8ml+ErzxV/s528+9zhKCPKOCLNbSCV5mg4Z2iRpxpbpEaQr48WuwiAyTRCiRcgR0IZnQ8BRcNvuw0K6+UjKm/Dy0msvLqZlUIS2ahUu"
    "vfEW3vgXf8/Vz38pli05d/oUlekJarOTzE+cZ378HLNj55ibGMVt1OhftBgnlw878fAv30N5ASdChy6YOiT6qDAOXikf5fu4rRaNWpUTI+PsH5mFkNsxMtPi"
    "zscPMjM1QbNewXddlOfjmyqIKI3USJyVtoPn+Zw8dJC16zfQN9AX5LqYhneRYVuuEKAUXpPYzz4M74sRqhBVUkZsgDka1cr4S/shcdXH87wY6UiYdmkzICmg"
    "1WyxeNkKFi9bxrljRxL9hE6IpjqWk0nj94ON3/daVOYqfOeHdzI2V2eu6VNx4ad3Pc6+PftxhMZ1m+jQdE5FZMoQSfB9r32GqiOin8Qp9dC7eBV+q0l1apz5"
    "iVHmJ8eYn56kVa8yfuYU42dOUeju5caXvIKX/c5bWHbRWhq1eiYGIBl1pJowU80gEg6c1MZh5SvSNthG8RG3zonkN3I+TfY2SUoOkXhcG3N9kUqENv/neZqB"
    "ngLb9o/x5//5AIdOTLFmeZl//+ObeNbWJczMNYj4mUIkUQdagtLB8+dYkulqnbNT8zQ9RS4ccUkhyedyTM/VuOWqjaxa2s++w2fJOYH0vFmvJUhwfI+CsahS"
    "ClsKfC340Bd3MD5Vp5i3QanAo+PELvY88B2kZeN7bpsBkDAIqZ0QTxmOQp9BL2t4YooOysSFxzQi62thnGPCUIOZ/EjddiDrJKg0XGMKUoF/MeQldCpdNkVa"
    "NmSDcVpsyOFKeBs6Y/JyQTlIpoDSYckr2gsJ84ZgwEKdKsDA1EW2O3HSxq3pjBQtfGb8JmO1TMhxhz/X6arxwq+WzsnTZMyS9AIH3W/82aOxhYdl2Zw9/AT7"
    "Hwnkr0orLCnJ2fDRr+3k4JlZesoOvk9ym1LW6gH50PM0W9avoqdYCCx+hYWvwfV8LK0o5h0+/j8/R6DYuHKAeq0RpEBq6CoX40A+08zMkhLX81G+xz+88wZe"
    "95z11Jst/u27O/iP7+2mq5DDEjrO90gomEYRodL7YzRbTuNGKilOomJUG5B//ECpQPYZSgdjxMIgNydZAwoTNTX/ro0RjIoeLGnhu0EeyW1v+F1e9Obfp6er"
    "zPGnn+ahO+9g50O/RoXcBr/Vwm02aNUqNOanmR0/y/lTR2k1G/QvWoJSoUsmOrPBqwSmVCrkCii8VpN6pcLsxDhnTp3mFw88xU8fO8SuU7N85ScPsXfnHqpT"
    "ozSqFTzXDf0sTFSNFDSs0aACOfThp3chrRwXX3M9bqOBFGFnFqarBrycwLALaSWlWNhRSsPBKEAU2seiwf4UFIkBZ0PF0k7PbcWjHIFIiMlRh+kH6iCtfK68"
    "+RbwWpw+cggnFyEDEepo5oQkh7AODaca9Rr4LR57aBsf+ufPs/3YFP/5pf/lu9/+X3SzQqNewWu5+L4XXL/QoMpsQYWQiZokQoosm3JvP7mcQ216nNnzp6nO"
    "TtFs1gJkKkTk5udmeeSeO9n+4IMcP3KU3sWLuO21v8VlN99Cq9kMi3fSIYIiDXWbk3SMXBHj5DPYhOFfxv6sQp8XbZD5EvsCaRBwgwMkO2ePfloLs9hLo0qu"
    "p+gpFzh6Zp4/+ff7eXzfGMP9Bf7xPTfx4hvXMTvvYtvJXpL8FTxvji0YG5/l4e37edblK5kbn0MjUEJzdnyaFUPdfOKv38j9D+9m98GTFHLgeS4vvOXS4Nnz"
    "XYPwGqCRQkp8H0qOxZnJOv/w5W14KGwrKLhK3f0c23U3R566M0wwTtRRWb7eBZSzHYh55vLRmem56NhnduI3dlRRxvHx5mhM04Hak6qTk546baWvRQciq5nr"
    "kx7yt41U2r2wOul40824WAAkkEILhI4Y/uFS1SLzMBrwyYVmOh0sxrXhgZHSCGf/3YyojwhrmRC21Cgr43ImzZsqZYx+ZA880/I8bfilDLg2vfp0J4kwtMsw"
    "MySb+KHX2bmgQloOM2Mn2HH3lyh194VulILebodP//ggv945Sn935LVhXCPTwTWEUC07h+t6XL15DcODvbhKBlkUKFqeS97SnDg3ztPHz1OwCSSP4ajCDxUE"
    "EcQthMBxJA1fYTuCj73nBl50zQpqjRb/9PUn+OYdh+nvcmKFQnoGaSpBMNz3DGWRYV8sOjwEKSDKGN0FYVpWIuPLrLlE7aPTbP8OxCdNMuYSUuK1GhTLJV7x"
    "jvdy5a3PZ25inPt+/jN2PLGN2clJ/GYzeO/QJCo4tIJu2XFyTJ0/y+HdT7F49XpK3X0oP234E0Pflgwj0FUY3e7RajSoz88zPT7GuRPH2L9zFwcPHuP85DzH"
    "9j/N1MgxqjPTtBqBQsVXKqOqMqXaUSS5j23bjJ0+zu4nt3Hpjc9meMnSgNCKDqHqwMDKzhex7BzSDsZEUUGk42dCxwWcMMyd4o44Mo/TJEhOKAf2GoEduOU4"
    "CMsK3FIjWTBgWYJGbZ7lay/i8mc9iyfuvZvx0fMBWqRURsqYOJVGa0p5Pr7r4jUb1OfnaFVnmRg5zdjUHOdPnaI1O0WzWgnMzNwWyvcSvotR46pwdBNZtEsR"
    "kH57BoboX7SUmdERZsbOhcWTlaxAkdjKSzQz46M8vX072x94kEq1xuW33MaNL34ZnhcUl/EmH4Uktm2bmSl/ZFKnE7pO6tBpKxqIw72ScbhI2H0RzyPVX8k4"
    "MC82F2sPfoj3RdfXlEsO07UWf/WfD/LQ7lHKRYf/7y1X8Zpnr2Ou6mPbFtGGLAwSpe8pustFPvXN+7hi4zJe9/Ir6XIsirbFm15yDfd/5a84PzrBn33sOwip"
    "kKrFfGWed/32C/ibd74Et+kZB1lSgInQAr23mOOpI1N87Gs7KRTswNnSkZR6etn3yA8YO7kfadkoz01iDxYgXGKqsZSBcKt2DmLaQjyDWHQoQHSHhjnVrCrD"
    "JyjjvSKNiYTJS+nU7CdFp2iHVUT0VkZzpjPliDZMKrMp6pmsmjhp/AI8lOScFubFVgaxRKBEpwvUgSCjdEeFS/Zw+U2KlRTE+Azoge6ET+l0tdhGajFRCp1h"
    "6Wa+o7iAAicFn0nZ5uaW9sGPiE6Bi2CzMc+22z+HJTV2voinYaC3yHd/fYpv3nWUvp4cnu/HO42OV3rQ7CgVEA+1EJRKRZ7cf5pcMc9fv/1lzM/VUEJghWFr"
    "vu9SsGFmvsK+I+coOqHBkFbhIRKgVIjAOr3uKvq6HP79fc/m2VtXMD7X5O++8Dg/fegEg32FYFZryJ9FxD7WWfc3lXRYkI6YN90TzWseI3xpDExFI4kI8lNJ"
    "UWMSRtHmwg+t180kWp34JwfFRpPu3j5e+YfvY8WWrRw/sJ9ffPfbnDp+jO6efrTr47Xc4P4qHdqTq7jg0Bpy+RJnjh5CS4fhVReF80srJXezQ4Oq4LMHoxTP"
    "dfFaDZrVeSpTk0ycPcvIsSOMnT1HtdZgdnyc+alJGpV5Wo1GYK2tEhJb1Bgk9yK0T9cK7bsIrdh258+Yn5nl2a98LaVymUZtDtsKXCI9XwfFhpMLxmnCTra1"
    "0FxNG86cRs8Us+MjLowI13b8GZWP77bwWvXgHlgWEGSSCCGxbCcOZLvt9W+kWqlw/+2/JJcvBM27o0SSVAABAABJREFUISFMLLQN8qBSKOXhey28ZoNmrUJj"
    "boZWrYrveXjNBtW5OVr1Oq4bWL2jlOHKSMzrUL4XXMuQwBp52PQvWYkGJs6OIEITKWFK8w3TJuW6QdZKzubMsSM8dvftTJwbYeNlV/Osl74cz/PjkRRaxCie"
    "0B0Idtok2uuUssJs5MxmKPbdIT3CQUZ/pmM1i8jkI+lYsinotPOmmgodHO55x8b1NX/7/x7mrsdOkrMV7339Jbz+OWuZq7SwRRr1jp4Fx7aptzQf+/KvWbVs"
    "Mf/3/7yWT3/oD3jPm27js9//Na/4o08zPjtPKSeYma3x4lsu4dJ1i5icqtDVVQx5KlbqIIswHdfzGegqcNfjZ/jyLw/R351HaijkHHK2w1N3/w+1uQmk7YRc"
    "mgzK0CGpVSwgPcxmh2SViqnYjIy3lcjIU+UCk/forrcLFXRnqa5ByDYPTHONiQU4K+kxjWjz4MkWRsn7XUA8Itq5Vx3i6UWcmpf1ntCZmyAyFVMnZqrI5KUs"
    "lGanM7LcNuVIh0j5JII5q4QxmOgd3Ndi8qcQFxy0iU5x01mZ1EL8Ym1cK8PxUqOxhMW22z/L3Pgxyr39+L6mr+yw/fA0H/nKTkp5mRHAJNkzliWpN116Sw7d"
    "pQLVhkfesRifreG7Lf7hz95Eq9Xknsf2U601cN0W9fkGb3319Rw6Nsoju47TVcqHhCkRWyNrBI4lqTV8lvQX+Pi7b+CyNQOcnW3wd194gkd2nwsTaVU6jZKM"
    "T4nQaR23NAxiwAhdy3qwkP450nb4Ou3+lFY/iXTRKcziI5tcKBI5oFY++WKRl779PQyuWMvxp3fz0C9+htts0D84jOU4zE6O4TUbgd1OFBBl6uLDDtdtNhha"
    "uQ4nX2Ds1PFwUzPmpwKcXB5feQHJMmWg5eN5Lr7rUp+fobdvgGWr17Lr8YeZn53F991APeLrWJUUmetEvAmRcklWoDwEmurMFM1qlfVX38CqjZsZO32KmYlx"
    "unqH2HTNdfQMDFGZm6dVr+IrPzXGii6X5/psuOxK1m7cwIFtj3Ls6DEsy2Jw8VIuuvQypicmOXP0UJwpE8l0tfLClGIL33MpdfezdN0mZifHqM3P0z0wxMvf"
    "+nbWX3IJP//G1zm692kK5a6AvKozKIrBdUxMgcJ1oZK8EyefY8u1N3J4zy4mRgJ/B+W5wYhLJ4Q6Qk8ZaVn4rVZY2Kdls4tXrUMIydnjh5AiKKKIPUREbKIW"
    "I8C+h5CCXCHH3PgoYyMn6e4fZM3GLZS7y5w8eBA759AWpGXuW9m1KszsepHKXYokiogFSIkZv6GsE2WUEtyxiRJJQZve+5KmwLYCcvA9T5yhv8fh4tV9XLVp"
    "GF8Jth8Yp1Cw4+dWxu6TAse2cX3FQ9sP88hTB7nroZ187tt38evHDmA5Njk7MDv0PcXnPvw2HnniIH//L98j39OFFAHKJEXiKiNMF1cFxYLFo3vHWDrUxdZ1"
    "/TSaPvlCnlpllpmxk6zYdD0x03FBWoZoKzIWSp0VXFjpEjdBRnDogqpOnRnFCsP521SiGDxGodPFY0AIzaRotzX+ndzCxcLjo07ikI5klIVHUQKBHVdrJNCI"
    "6ECfSVXNsYVuAo+KjLVrx2yTTtaq2arPZLhmKjeMP4thw9i7PWMWJkhBXubhFStoYMHRSSc1iu4gg5UZrkiqmEkLekBrLMtm78Pf5/zR7fQOLcX3FOWCzdnJ"
    "Oh/5yk6EDPwOtEre0fTs931Nb8nhix97F5//9n388M7tLFnST285xxd+cB8rhnr4x/e+lhdev5mv//wR5mdrbNmwmDOj43zvzl309ZbxI1g5NEACsCRUGh7L"
    "hwp8/F03sGFZD6cmqvz15x9n37FpBrrzgRJFB2xvYXQ9IpnPxQoFUt23DBUhJhtbda61E9JQYr2s0wTm2L0uQ+VOTSUFoQ2WDlUqMh7+R52X57s897fewtI1"
    "Gzi2dw+P/eoXSO3HRKl6ZRa3VokLjXQQl0gkaVKgPJ+ZsXMMLFuB5eSTDir8Xc9t0WrUsGwr4Wr5Pn7AfAwQJ9+n1ajRqNdwPZdmoxmGggm0H5FNE7myUgHJ"
    "VmjTCC4ojHzPJVcosWjtJsgVOLlvD+su2crr3/sXPHHPnSxZuZrl69fTajSZn7uLmdERpBD4qayTaGUnrqRCxhN0IxtGGrfBkOlqje+6iHwe5bssuWg9F11+"
    "DbaTp5R3uOUlL2dg8TCPPfgAp0+dobuvj2alGkpDic3ddEqxljwX2g/GIEoIZKiW8T0/lI8HaIvy/QA2jsZBoeeN1hppO/iuhwrVWNH6kpZEyjx2oUBlbhq/"
    "VcdxEj+H0HokmY1rHRQbWtGoViiUSuQKRRrz8+x+5AGK5TJbr38Ws5NT7H78EQqlItr3E0VIaipp2GgbqrpIfRKZ6ZkwNnFIXEKtTu2xBidEhDuWMAzYTNPe"
    "NsKoSBFO4mc2GDeEhM+84OPf3Emj5fLG29bxrldvxrHgq3ccorsryFKKP5nQ8ffo7SnQaraoVz0K+RylQgFfKSwhmJ5t8MaXXsP6FUP8wV9/ifLiIWzLRto2"
    "zYaLxsJxJL4fkNuF4USrfCjlLf7t27tYvSTP5hV9VOs+Xb39jJ8+wNMPfI/LnvtmfM8NTPoyqeWdXEhFBr2Whr4olZXVyWMqjqQXqRGvyMpkDWVdpwyclAgj"
    "zlJJdT/tUvxUIHl4DyQpomhbsSQ61wzaUJK2NfG/QR5NFI/QNodhAdJLO26iF+ZumBfxmSiUphVrh5S5TgQdgcGy1iLl5BizdrMAYQe//LYRy2/IWNXZCOpM"
    "YRZnVkRkLhU4iZ459AR7H/kRXf2LwgIksOv9yFd3MTFXp+jIJCcjnE8ajzmtZourL15BX3eJT//927jx8jWcH5tCao9C3ubvP/MjXvKuf2PP4RFuuWozl229"
    "iPu3H+GrP9tFuVxKJr1CxhkuUkKt4TLUZfPRt1/HuqXdHD4/z59+5mH2HZ+mr8vB9fwkXdUcYRg5PCqy3Y3QORlJ9kLSZxadCl9H2nbi72DKolPR2qZqQcem"
    "WtkMHpFadzpFiIv+u7QkzXqNa57/YjZedT3jp0+z/b67gy0k7Ji18nCr8/jNWiAfjWFckQrmi4AXiaQyNYG07dgoShpx0QJBq15HeQph2Ul+i1Ih6uHjez6u"
    "58VeGJ7rBSoM38dXXjIuCQsU285j5xy08sJRaDgiUIr+patYtfVa+pauxPM89jz2AL/8+v+w94ltXP2c27jo4kvwm01KpRLl7m4atWoylom6Rp2Y9kQmdbHJ"
    "VMqpNURcYka7MEz6NK16jUKpm0UrViKl4IpbbuOWV/8WLa/Fr374Iw7u3svqDVtYveVy+pcsDUPWdKhwESkCfzTKiiSmQV5LyIuJCi8hwnsYylBDBU1UlSml"
    "yBWKWFLgNetJNygl0gl8UWwnh2XbNGsVhNIG8pOMeWIXWQGWHawPv1UPJNtCYOcL1Cvz7N32GNVKjStvuZXhZcvxWi2itEGd6gCNgDDzz3XCu0ksAswuFWO8"
    "aUjQDZM1Mt4PWpiCHJ0QkCOZgzYi7zWZBG8V1/9KBWPZ7nKeT/9gL9+55yiOpXjHKzby1hdtYG6+GSfBmlxA7fv4rhdIk8Nn3w8PLU9ryuU8f/POl/ONnz/J"
    "6fEq5XKJuqsYm5jjZc/dyuqlvbRcP2xkkoTSiGviWBLP9fjwF3cwU2mSz1kIDb0DQxzfcy+nDzyKZTvBOC1jetWJ2Jnd7KMR70IkUDM5dSGZrb7A6ZIYenVQ"
    "m5jERKHTHEgjGTsOXjQ9P4Q5I9M84xHfaVRC2va8rcle4MWECBGOVMVEu4lWx4TJdjS8jUzDQn/ewQ00VQ2aqXc6HZUboSkmPV8LbSQyisQTA905yKYDayf+"
    "PZ3AWQrVBqNl/fVTATWig9VJCN0LaTM/fZ7td3yOUndP0LH4inLB4aPf2MNTRycZ6MqHoUVhFS0M4m14ujk5wT3bjnD3mz/Mp/7urfziS+/nD97/3/z4zicZ"
    "HOyju6vIQ7uOcc/2I8iQWJTPO/T3duGHPgsRw53QPbLe9OktW3z0ndeycUU/B0dm+evPPsbp0Xn6Snk8108C60Ro6W12niKNdkSbV5LGq0KHRBJXRQO2TZGy"
    "klYlgNYjPxNIFcVxsJOZJpYxqhNZK+/wvVutJkvWXMS1L3gZ9cocTz54P61GHcdxaNUbgZmU79Fq1PBdF+k4KM81MgIMYp7WqHDN1KtzCCEp9/TSqs6HZLwQ"
    "ig9D9VrNBk6hQKvhx+hScFB6MV/M8z0QgQeErzRWNDpRKvQdCQ7a/sXLUHhMnz0Dvgci6Pj6l69hyUWbaFSrnD16iOr0RNDxa8WpQ/s5tPNxXvO2d1Ds6kKG"
    "Dp/F7h5a1dmQmBvFz6dhfmmQus2DTGnTJDByG7WCMYTW9C9fw/rLr2HRspVorZgdO8v2u3/GmWNHaTSaFEtlZkpluvoHGVq5mny5xPip4yjfR0orDguMsml0"
    "6BVjao+UkomvghSJHNkPxyBRjLryyRVK2Pk8tbmZRFUVZsLYTi40A3OwLBvXbcUOpUl0gTac13UYyCcRjoNQgVuMnQs4KvliicnR8xzavYOt193IVc++lXt+"
    "8N1k2UZcHJHYkSsjdyaV1k3yOzoT2mgGTyb7nxHElWn8RLjWZAYFwSRuS5FC1KIGz4xwiF9HQ3cpx2d+uB+l4M0vXMvbX74OpRRf+9VhertygXOoirxzMCS8"
    "EaIrkJZgZmqWP3j9cxgcGOBT3/w1Con2FZdtWsGzrt3Ec2+4hHsefIr//vYDOJYVE9gxFB6+HyTgHh+t8tGvP8Un3nMTrheoAIvlHnbd/016BlfQO7wyINBb"
    "Vhuq3wnZwECJoHOqeTy6EGlllxntEI+rOvhfdVaMZEb4nSgUWXM5nYwyVCcrh2wejFkwmeSd7GQigZdNgVViZSFE+3oM/7+dkrcafAvZibi5UEKNEAv/5wWK"
    "C72Q02gqPr6dQJqGcUIylAHziZgFHn15SeKy1m5BbipN0smrzxy8lv08pmQrHbASHDrbbv882m+Rz3fjtjwGegt85+5j/OKR0wz05IOwImFWjsF7BkVBg1qt"
    "QXe5QMGxEIUc7/3w1zh2apSv/Mt7+McV3+eTX76T3r4eersKaG3FhNOoA0w81pJKvt7y6CoIPvrOG9i6dpBj52eCYmOsQk8ph+f58fpKLTiRsfONOl2yXguJ"
    "813MEYo2sZBYGXdvIh3iFh/YJvciVSlKY/NPzz6F6dxh/icRmCtd9/yXUSiVeOqB+xkdOU2xkA9n+cHIRccdc0jyVCqUFyaHXNCRhrJPK1C74LsUu3qYy+UC"
    "JWMrMPESoXFKsD4dLEfhReqXJMQItMZrtbBtO7AcVx6WFAHZWCk0fhjyF+SXCBU4OjaqFVCKUm8fi1aupV6pcP74Idx6NVBAWcEYMZ/Pcf7kcX70lS9yza3P"
    "o1ptMj9fZe3WKzh/9CAz50cCmWx48MjQw0EAtinTC0cUykSe4vwOGax7K8fKzVtZuWkL2lcc3PkkE+dOMXriGI3KHPlSF4VcAeW1mJ+uMTd+jmJPDwNLV7J4"
    "7QZGTxwNivXoMwgVcjESEzkZLc7IUt22EQLqtVpY7CuDVKuRlkOuWKI6OxOb7klpIW0HJ5fHKRSQlkO+WMYp5INNspAPPoPnBVkzBgE64KoExFNLCKx8gXLv"
    "AEor3KaL7yucfIETh/YzvHQpKy/ayNpLLuHo7l3kC8UgQkCYhk6Z7pYM4hcbyqg2Ll30DERxANn5eUpCGQMlGQTZULfolONzGEWvEzQ8In3HPQyB8+dnfrQP"
    "5fv87ovW845Xbqblab5192F6e/L4vs58J2KYXwOtlke5lOfdb3oeX/vRwywZ6uF1L72Ggb5uXNdn79HzfPVHX6TVqFPI2WFBGSE0EkNNiudr+rtyPLRrlC/8"
    "bD/ves1lzFSa5PIOLa/JE3d9kee87v1YjhPPktqnC6LDKD0J/FyI16GNv8V3JXPOxM1LBxGGSBGAdWiWqDNeUCKV2ptFWLRhOtixKDGTaDOITJg4ZCBmIrP3"
    "6o4a4iR/rV04Eoa3LRAln43azbJatW6bF4kORUg2XG1BSVCHwkKEG0aK/JSqtkSKvJfyyzDGEBf2/EiqsfgmiU621L9ZISXClj/q/LWvkZbNrge+w9SZffQO"
    "UlmFJPZCwaDQeFz3oKeEnsTbXixEHNc46nSxne3DRRS+xlNhj/n74zkFiScVUKreDNqPRtiXbqiAPCBhHPliMriAEYix98S4uz1bRPJzTAL19MBX4HJFqmye"
    "O29AIbxCIca4ohhrDDSqmfr047gnIdKXMyym7qJrb3XAduJoZR8QMCqaVvVztB4DdfFlA6vWBDRWKUb4GothEBBJDZVUSfm1jALl+ij9kCZxoklWohvVyJ+l"
    "6Zds3vvYFezSQ0IZjV1QazOyaRZph5UN+TkuzOdmKjj66XwcRyT5QyWjLMYpIdV2yAiDeB8y2khyoLKqVX/3HMAwpE9p3AE3SMzB9hJXiMp2RlQqDkqD4qNk"
    "61bkGIZ95g3Dm8eBHkntszKBobEIVMNVZ4LJoc1xvq66KMsRzlSls7pk+PhDSUH00sgEprEJBRrLVShIjJHfJKKCw+UCOVFlTIRnclkEBXfFEeV1jiOK4EJj"
    "+bNNYitxd9KSOyb0SlHmDj4AMZUi0WD8hZmi6k4BRZPwLYLBLqzCuYhEUq92t+Gg4tFnDWXTjSWJcLA6HdwTPWxeZLLAquBCIlLcRZUQihs2Attuj/kWixgI"
    "Z2iADx/nFFtGBRTtqIuLS5U999PSaI01neyp4AlZdlETjIIHJbFiMMXryPk9PesHmms8BRZ0jZWdvcMdTctFHkWOSjLjCuIgEQ/NLAkLtMtwMCM021g10J46"
    "rewn0hvv54g4O3OW+X6L7K2k4izC5tS1mhp5lOea8D7onu/Isi6TgKDR1iD7SqtUemkV/JAny4N4SFgVGhU/KMhC2GZI2wLIqsmjIsfB98JW0IMIk8NaHRAW"
    "p9YxOHtHiyL57jRPDSMUo1qsoTk0O0UMKD04DTZGcRZJG27lyISIQgwGjo0FiU9oxc0HXwgH5coiMD5IotiAZ+BPf5hxFOPUDQ8oGRJ/aX2N13FO5R1yXwdI"
    "mMdmmq5rizj24dsA6ZNxqTBcAsi8S0ORqg3QZ0xQhWeYxFtqaXzFqaOnadkjpWDmZ+10BJ9I1mm1glfx4DEGZvo0QXjg+80OqqUJe4xC1KQTmZ4MeiDOzgcR"
    "AyBGBlhdex6TNWf76pOY0cjSvdFV8pFL495gXbJZWRpFwPx5SESJNe2JlKm8M0YmeV9xgeENPVCwwlHujAsRNA3iDeLdM5GqlsDm/aijAY6D+XBIay3IpjAS"
    "8tWB6TwqIodS6+pFsBfQRGXZE+ZX1yI0SDrK+/qGaGZ5ZD6ajQgZPckgqJDTKrej+jNWCImFROGzZNXIntbaFYMhTfa8TEiBgGIPN67lkAl4fR6LnjkgciTK"
    "M7GjbIrOPHPYr1YTsIO7XbMgZvPaUc2eYjOPkxtREReg2jxZ9T4OZnw4SUkSrCvvc6yLbr68wAh8RjhB//g+NON2K/kkO/VNvzbNoXuNGe+Loy1FVGLZvZjM"
    "Kx0SQs2YZcT3OeUSwI1pCHr1BQiH5kCBwsj4LEJcYK1Dfc3YHCem/NUn5N4aQq8m0j848MnUBjrnpoe/QzIxGuIws85dk0tAstwf7n5OHKoQmuZaEQqNn7LU"
    "Edx2Wc2fH7lx0rnHsLOeaI6PUV24WwNpXTymYmCqks7P1M76hOyxDbsVPOxiJlQi4RPirOZ7tolEmYT9DQKqYTExO3yHwtGFV9NQZTHqTB47ufvYf6YcMlKC"
    "NHMkKCiAeBXNm1mbs++zcHHTY4FFeA0Ir07FBqI2K8+GAbfbDCccBM6xFhp1Qj5Is0cqoPCHaG/U/cj9CjcGaWxzQiNRPusQVEZ7CHTSs6DiEQk7hHrarxql"
    "7zK8zk4ErIxzIsDaJnPU0zkWCd+1shALY+UfPS/VwVoUXhhu5kxyW5SJtXghYFOmAgzGL3Q+UVidDzfo7AxrBaJlyRadfSZ4fG4FpjXHdv5mlop6vNPTLtcl"
    "ptcDlcEmSGg1trC9VHYe4jNHoRs8qZnVbNqjNnBhggYcmJYpTIU01iqiDc7WNiqYhARVybohkDZ02wGmSlGiJt2rYhYWp0L1GBjtZD9zPgDya1SWNWd7Y7hA"
    "3irpu8GhF/OtyZgGByUUQBbKM5mFUfdDYLCK0xUx9RORaAp6S2OWELk35PSI7XBCsnRmGxo1ZqkrJVfNYw5DiScjWhrDAhp4LcS/GX4ZNmPWI4dMRWQ9zpKb"
    "rCZveiGqjd5nmRR0UDICMrsiT2836w3ogMVMSkdXOsZxZ4LbHLGoMsITUZOHUZ6+L1p9z99t4ri7UIQMqS4mn8rIRJJRp4pEI79nUrGOolARwqqFOUCa2VVO"
    "UmHT/RhUKWY8lm3kTNvYI0KnBVRBSMMlbskUUx+i1vA5kq1MKtGzoqQT4d5GN1JUalIWeLQLIawor3vSKAWWLjg3Fe+BkGdAc2IGXQDM0siagjmDnFZQsPnm"
    "NVfDslVRFhnbd/ONzJYXxFO3cHFerTegLFsWqlVO03Ps6quNrOI5+HyoDwsjjtLCvj5fra7RjiTRvf3dHQsg4vYXKx6MMr29wBUiTV5ZyspkqvmUuWuh2O2M"
    "cewQpnTSPpY6wqbJziB6RcFYNs5TapPxPJGWoB8a+EKwYmKS8vJqb4NgNTsOlP4FhIWRV0jS9k79ficVdtJnw6kmLf5LQaE5AZ5lxQa9V49Rn+Zlc0Rl4dYe"
    "j4ly0Pz6oIPzLDw9F9Mxx16H9fwY7hSnDPNCaBp8t2AOD7qCaiy35qkRaVQPEs4IXf7TGo813MNcRyTgYBrkbGnErWLLbUZUWvJZnrkphkVGHBeC54wUGMs2"
    "+2dqOOPujYAHeWYMFAes75GoCAMGzqFwJn83IqQ7eS84OCSbIw/DGQXQlAycuEi6tcYuRLqsmJAcV7ijwbfxEZpXdb86Au3/bqMAKqY7lLJ+ZIuGhf/HLAi4"
    "yPTFxo9NwdgzD8u8wXRY12dKpcmpBRGXjQDlmfC4jYvnNrnqFuTsqzq5BtcAX6vBeROJMF2oJldFUqrAiwJFCtmUFlvyD4oiI/2lL7p7Ubgso+QR/aDEOiAv"
    "jdwShkFjIc2lStkj5T/cVBXLbJfu5ZB4LwtEx8AKOFY3Kmqoa3TgFvm9+vWAKj+UbCrVqbtNrwo44EYx1npS6UWGtQP4FcaS5T46k3Ecq2yMbNYjKiM1dC7k"
    "ivIIS3TI/h7RWdnIIVEPGA9mWEvBynLeaSFMc1FdGJHNgzpDnFAXSEr1PpKgYlUIq6TYlV4rpJxHORuhK3Ulft6LIC5y6rAIgZwclb4+Dhm9cVfGvZMicfg+"
    "jeBeqzkI2LFXls4MgI/XOkJGAzqyheeFEbupxZgWZZbGv6qk88L22RPRekCV87sgcRR9UMRwyS9po4WRoVeFwJB5X5HZhHHvLP804NkYY4hhZI5I7qE2DcpU"
    "jYfFlyoMmbMh3CyR7q4a0iiUMhhims4rty26cMDI3XFUGPe/S/sAzKA76Xwd8CbqFJFExwIlp4wjWASsdHHakye/kau1A/xJaPeh1OcoCLFrj6s0pgLCqceF"
    "JS/zMXQyoQxbdSLTvVIzQIKtmzgueKZiRshh4HOWnStbX49BIkpNerVglAOSE/5k5KMaYz1GIoTr5GmRmbrJgeA+ED5zXxNJdy6uoXKEqYLx1k7ykaFjp1gA"
    "Iw9rHEhi5BMA9DnzJEjlAJttpdMOZcKKWOeafUAnN7vcg+SblQIFUixnDWWHwIRzM+XiBjvsCOMEZVi/nYzqIfkKgygatBGjjbsRMjh5KwGGWBE16Ro9aco9"
    "OpSSishTTafHewg8uvyIpylsDPfUyedqRDhjOxDdYqdHg0Ue57iZHSM0DFZj59Ogh4x5Uq4EoTTKvnbiCfMBP+fkh4lUUehSaXSDBljJBfRaLz6LDIfU34p+"
    "3z0/InI2w1xznsYb+flgsyh3ud9WF4SZC6URGDZ8bXD9ebOEpCuSZoXipNr/kGDvyfkYBp2KiiSDReFL9BvRkNgv55sz8npG2iAVIMYBTxEH23GFjnem9D9c"
    "EmGtdkhVE8nOYzsu/g9qxjTSA11YCTWBM0odUFlNyrH2Rkm85589k8jizn1cAtTMi+ax9vaSmN+6C+JqkR8AHUaYdkkVCrMDMwqHUYsFkuN/AwyRjVLJZcWY"
    "Xwi3hbU5TXsjEZ9WvBHkn5GuvDUiNkVMv/EAYyvqSPphenSegxP0hw+MS3yg6yLRmWYDG11a7Hpt2lXlX4XG0UPCFN6VlBeHFeNzo23IP1GPgg752kRUDJ0V"
    "HGzYL1tx9mBAV0fpOLyrcDpX5qA1f/opBNn4d7KmN6fRCW6ug4bobLdvesiPlGaYFdOMeJL+Epq3eCR87NON58Y+D75RiKA6o4eyIKERdadYUPkkHk5g2KVm"
    "8LHRN0nS9KsYOq7N1677UHFLHF2XxXOjKdoh30mNp6x4jQphauQdpKgt/uYcywXJbW0832P9QBPAYhIx5XL+uz5RHUiQO1mxE7LBsMn5jLbGlSgqA9UyM4wP"
    "VbmfcQ8+A+qE27iX6zVsh6wpZ6wgWWL8RYX6r8i7VggAonbzVukpoRnJv9lzMYVNbzHoiR2JVIuvSkhi9QRgNvF51CQjFSad6U20zVlMDpXXJtwXbu+ilwe2"
    "wnHSHVRQGf0ckp6OI9nNIl9CTUkqL4uSKFtZ5ybioBEDHUNs8HNPp1GBDyPKTdakuu5ciDR3J4mVdK3iAbEeK03HzenpgRuXCbFVO4+o4fqFw2dK4I0l+Hgl"
    "23K3eSi7HY3rKvMeRaBkDhTlPYXNJ9TlNwx9kRgRqaPa+TsyEY7MzKQFQRdVtvRnd1VEqGOvnUpAoSmy7TNR2MNoLq9pAohPLEnjypgXUiYO8R1D/WgfVHUM"
    "b9qq5iIQFjhkyHkSbIVSkpNc01DVUwOiPIcP4go4xLz3UrObATr5fdxD+EkB0toZKLfzm9iNgE1I+xaJm0LS64oIT1uhl2TGOYbUUTDfF8dRMV5nRwK2eq8E"
    "FYq4F3jBMVzthzOkMKfABibaquGhjLBpn1COomX/qjrCvh4zcREkllqFJLoOZbVpZeHGNhNq7NlRZnEUXV3DNFphlUqkqj2iSIut6ptVlC3abS/SWm0DN42q"
    "yWpznuGTT8l6vCjOC7mpysQVlPuII4VW1VCY5SRdRCnE7W/JOSUZUp1RUSEqOIsNaX0TYL1wjE0++iHqDNMuB7+eF5yVQlrmdQdROddmnkO/BwcVU4gGje75"
    "OiRxUqVELM0wQOOwEbWuenwcUR0oH+NxUwyMXeyDwwubdg52coh9JjjXvSInWUqYKfKEQmSsymDX520Utzhmw8+Ntogm4x+bUnbzBUwMbvnUlQLHYlxJZz5Y"
    "J5L6OASD8ofGQRYYiXYFTrlqQ0KIyuL+CuPOTjie7o0M6xsiYGHsSaEeRS5BajDe6HklTQ53XzAklUOB1EA29YI+9xrbML1FrcFnhomTJQF/BzNBwobjJVKc"
    "cOSI4wjBbiVCYrVrL8fI8Z56wuHs2BZn4cmPgp8VLnjYHVpVc56Tyb04e6t5U8/m2SSQk9ePGkZKWChzJieixffMSnWmCZKCHisWU1FXJrOPgqNf/OZkBBYV"
    "bzQdqlHMV8Q1zlhVEEq8sliiHr5BcohoiIvaakkXXwidQgY4UORxhWa5rBa7FxVu2ipWKMo7ibo7gq8Jb4W6FOgcfPIBSlGgMw8FUxgrhI0MYJIOu0bOqPCX"
    "5yuARDYTZ8F7AWHWqgiEAsKFlzCr94Jsajk9eMKmcOy6Ub4KX2NdtAXYAXNuFwPWtJYqn3ZX1geBWSVxWb1xsIDwPVKaaJTo8Eu/5gXZLhhtUMtuUwdDrVMk"
    "PsdlEyTTpYhSgjgehzbrJULIZMNzF/IuEDtZ2OFzxl4DOqNhOlSVpjwH4xGCLw5Z9N9wr4p0T4He5uhXAqoty26nydHYqngIqhil65f7Kft8mWFlewOssHfJ"
    "/sn5LxWE774uczFcGqd6US7349XiKBAVLpLYENM2qGGkYkXEBDYVQaHO2yRDtsxxMfGjKfpCLG5X4MIoOHZEyKhuagXXCKPTQ53XWPNLgUSWRuCLg60m6TVE"
    "Owr4UclNO1LU9tAPeW2rg37w4MewIR1xtNYGZF0N+lYMZx7ZWK38EXIUI1bsB43QXqU4X3mBKNcEC4WVqQwVGyMNstaQRznEW6z5tBZ95I6sEBQ0BQtCDACp"
    "EK+Nvm0fQOod1sExTc/QvXXOqQ04MOVEhgieLnGPrAqJ3LKNJNijnhpHlBcybun8+r6FsZUfA7TuKFRzDY1y8cXyNXKXxichnCHjIoYlGmf8t00r7WtDuugy"
    "199Bg0XiiMhjgeWYsJgxjVEXNlSizli5hC6TtYk/7jTLqtZKRh5o+JYTRKXQCSkssKAOR4aEPluAMiFBFkiEUaC52cQjFwZeoaCJXNzoO/KeXigFK99J8b/h"
    "QkDdqhfZYr575rwMMyVPIbcUUNpRitioIgm16Mu9GitD4+aFB1flh1WPnoBFcmP0U8piU1XSYOOtKsgumxNP+RW7clTqwWMSLUJUJpsSAUUiu0QPRId8XyWv"
    "915feOtHMevN+vNNnsulGpmLNsCdMkp/+aqjN8vZD1vGdzEr5PmfMZEQ0y4LNROZiEk2DKFdMpIKIdSRaY7wWirEbdXsOCIUJS8MXfQoLm0JTXXFTaTRAiMt"
    "JFmt1qSYmqVHBTeWyKk5pYAuAx5Toqv50qrEsWxbT2oZY6VYYLor8yIvCiySNbmA8orJR2NAh5C9gM3ZSwKgG5tzoZ38HA0cRjIJTCy1VYaKbQ//6hzS30UC"
    "M0vKTcY5vqB0vWkSRLb6vt4OY5kKn8e6WKwtXb6rmHBxYIYslZDx6/nyPA5IdgmD4NxJvmvTxdk0G6AwlytFgd7sclGUK5GCPqfcsyxAHByyfYPCRKHYDOeg"
    "UV9QHxSp2bmpuoxvGaGYvIyE2Mpj+q6hWToXObyNO+LQmc/iUEbC250vxSTwRLZAFdc4DfOpyS9GWRuOJNUblCChNZYD6pQnYRhu5m+S8ar6R5GUXbBcQgIj"
    "5TqQjrwgrlXvkWGvWGxy+gGAi+I3Iza3RPgqCyUXVn4signPZ21UBCyr19X83vWIKwX1aUynSZbNolhYzThCyb6WOS+z6NIDclG3GMrNGek1Qk+kI5XNDxEw"
    "L64zQq6B/AuUAJv4pkCKcOqiC9VGydLHzbAsOtDTgutsNyvJgniweho7GptpHWyulQ4gZzVX+p7iUlU9WzkX6K628N0WUc8tFzyz8uDGw9oyeY6IjoWMlngY"
    "SUV35RQdB8ckrByVFwGgVTNIkD4BQftDcYdmbRtTt2W8xWr0PoM+MyExtOgfa1nyyvD13NM5jYVFLi4KDw0Zr1QFUK70g6cWNwWHuxekUak4l4fhzlxl59VB"
    "VZlT6JNXDofIAAY2ela28KahpmPc8jJ+VSXdrRJwh5W4WcExyXro0oWuQkPeVJFop7VGbbIxzBKm1HEPohyBOQdYcEXpt2I3SBeyxVfcoMGUDi8O8iiq8WLz"
    "RpQCoO2yiAmp2ourGIULLgbDWVSOu5Hs1FePkomB1+QjFA+zFMmpyAWC6Ayj0+ut5Ye89K7gAIKyysoP2HzTXSncas0sfe9c1Dnl6aTP7V4ncI58ktggZK6T"
    "mOXAennILzfcReR7gSTtANa3EY7VQbeIqjIaIcp92ciQHA7NIBn6oulamT2OojVqsy/t3FeoRhGf/mYlRsVjHm3kdO5a3WElF4/4f/C8jn1OYu7r8xN8jshJ"
    "mv1IXMcpRXHtRQEXVdEZxinApZHXWvyxFFeYmb+OIypo2m8MpWiDoDez7q0Ec1aALYWtnUYSZ2mWTXPIxls2Lx3NAFufq2bc5eY8cfC/K8XIkWdX3IUCh2Tz"
    "UKg0y97kxaRRzj/5pxz/GLu11pd2PScEscLIi7h+saMt9UY530TTE6NgVpRkW9dDN1uC0jjFcxe0GsXojFuvewVX1kUBX2+SKwere7AbIy5UpKnz0p6/GjFR"
    "amNSfMixW8lDXeR+sN8i7UgRI7dKuXFxXvoBK8/q7Oa6k6skETdXjmK96ePv4GsLsdQdbZB8OMwajgwtsumW+Ct4NaRSYquOCkIMlSxzRtI93oz2VjEUhDr7"
    "lPsaup1KIVk1g/hAJZ3UApmN5HAMfDq3EmHbIZNuOVm1zPjw94uy7fifdfY1SkI8+ViPFiqEVfwq6O/hnAwck5k4iOq1xsLO/a19PI/Zcc3mvTk10sFOpO+N"
    "VPAvVxbh1XxsRfJJpLWCN0Cafp33rTkKLgiCCWt71akuYTmFDn3NevEVD+OOpbwpOCqSUdUJ7NQD9iY3hb1L+ARZqhREZhalCoEX3IGMaMuOsXzdUfmBh8+0"
    "B75e7CKAFuS2FCttZWeE/hgRao6VYfFSyqvMD5p1IwEUjjwdWxSSHeR9GMl/88N417SVXiKK7CvSXqBreeOah1SU6UlZPq4ohlf2YR4JGA86GBGy0DFEUVSD"
    "YVVdWPoOmaeGga8BIxIa2VCNFarXICtyK25KURS35lNVJuPUpdoCRsoHZPUkUndxIK6Q1n7okdx6hczGAg1QNFzH6Kuo9hLZFruEyjthe9g6TZ5jUZi8izQt"
    "c1Vig15rHEXxOUMJswWno6Mch/79Bnmq0P13GlxMYldVylLF2QsO3Qh6dseymkwVeT70dC42Qtz6fuBe8dyJrMfe/YndshQMowToWFmvGpvIlEWEnY59lQ03"
    "K27eNNXakCxTUuNGI64zUYPv4cLpcB3vWNEN4aa1GINgDgMndRo7wyK6uZrD4gMeluDD9P/YCe8GSUQyXujxzQoTIeFwJO6JE3ElzeohadbLObdpfvO64IB/"
    "J1TujiRUnb2FC21YMr5Nhy0WPjz2seRwqnOSqeJRKeViM/Kc1nxm5BxFzDvPtOuikEeACcp2Lwz5ZIVJdz71/Gu1AvkdyDiueRujahpXFuPWdw443+SMJCQ2"
    "styxPJAWhcewqT6OWXDQryyaLEPezeb7aY6UIDtnkFrMTBmxNA8ZKyCqvbqOGTCK8tqsMr/sxlgtChSlbCQRHSnVMoLsLFJvERGpEKl0Zqh7q6pUd2DD3QS/"
    "eL3+/0+LzAeIIoZvnbCKmRDVPIhtzGlj1osqB00ZjyyzQKzEGKrSCtj5QLvej8Y8htpr7IaZSEj8kBKGW50C+VpxHQVKAler78gui87VMy1A/LzXWCjBevLf"
    "Yw6I5jtQxKDaI4UbxTwIrIxvx0VbW7cPg6Mw2FzAohzunZcbtadEzKHyGeZvMx1X/SOSvbUkdHbZrEMMPMOZm0Oh/85IH70A7ms20ZpTJoj6NaAMMiy2B5Kj"
    "qyIUzAbK0D4FY/vy/LqseGpjLLPiI8z/P8eMTnwXm3bXxRadDprRQWVOF/+sqDM4wGiOPYtrl/xp9LNAVXqmuS74Bs6+Inj/WvFzkfUdRTFWRALgyEyCv6yI"
    "UEdTL9ZN3ZMb6ICRvW8cpS7jPqF84HORvCywkXBMvOaxaqXkwtcKQFmDlD47SkBRerwzhhBUi64zZod0kYFNldxS9UH3sm7w1YOlJKlWxdRVKK7ScpdF2U6B"
    "WKDxFFdRLq3r/Y+IsIhbiIgXn+VRiGiDvQoZs4LkVjxIp37foGILm0xNRURuuApmSY7ZgA1uRVW8tf714kBNDn2+r4QXSXuKdpDBj9W3KTZFIR6GUSwU7fbG"
    "ZlqMiJQIhLN60oz3YsEUKa8DitgC3JmUiJW/CbmyIuPJPDiqLh8Nz+ihAQIzWYVHUs9U/K53IMj0IYAMQdODS3XF66S2s578CcBhwOdgqdzBHjj5GNS/N6Wq"
    "sR/vrLwUFqZdmDrMRWYtf102V7iXGMvR0QJ+shnANTc8Zfc4IqRWoT38/xN8bXPtSDEnME2+VD5N6UyyLWqfHR7FhDq7Vgmrq/u0gPYzioSeOw5nLYw/o7JI"
    "r/jPkZdO2BZBWO6lwZkyO/+iu4783T27RFj/Jsm34qXtzo7KV0mdRrlIj/evZ9pLI+UJvf19ioLmyc+BJ4KYVjGJWatjCT3YlAtQ+goobNqzO0CWhtMTHa0o"
    "yzp8UYV5mbyIro28+S7CP6rvW4yJVvOx8furaxaruJ9IkGPFpzDt3vB9VsWOmFiZbk6mkdo5G0WVNssDwtn3QtUww0p5qWcEX395XYseEz+Lj+aAUJR7hpdE"
    "xI6WJf4QJr96nJ14YcCWnwXtSHuI29XNg+c9jjbIll1yafpBOrpX2OQPq31ZsLB3RcaSU++0D6/GU/wJOb5ei+bLXTwXHq4defBhO8aMATJ3HuONiykQc+98"
    "HXNzfBoThguKJC1oW44KFLZ2hmEQaSsi18e1xXEvcHw0jA3JnwCYZa8hCZ8JkS3rdzgg/IdyeZhpm3gYDh1GlMqQSWoKiywLJvlyJHWSCz8lnVFFM4OoFCWA"
    "V1263zQGRfMC+APxcLRQtw0nouYxWZmzUsqX0/co1Gpyxq5cwrEgPPpeRoToWKpPtoWFr0eDT/XTqAk080Ic0AVQJ150SQkuouTX3OFqWFnN1M7OhHjt1Trd"
    "yEYZXU+XdLHcDRRw+1aCpH+mwUGQMXNEjvMt0QqwfE5Qml/plQuL5dIFr284xzFg7lgUU1e2O3VtAxHywvK4Q6JYrXcEjGwcTdQFYPddlIYzahrFeLYe9Y25"
    "pk2EDD9zT+gseBfnwd3K8QXZ8o/oaS5U3aTbXvKfsDuBAPRmI5eEG0suQM6NtY3GcvJxQmy4qzGJlxEE7D7VCYvelwA9C+NOkOIp0kGAB2YjNMOLZ2/6exBz"
    "n5jzc2bdevItcmBM5Y4OZlTX+Mr5Zy19pgrEjOkcDPeGGfvc3Lg2NcHfyfW15mDHrHKipDFxBctDsRN12mkxCGFOHPgylSgiIWFWNEJGSrbS80ak0PSk4/4b"
    "kRRs4zqh3BTPg93ECFU4kqXEezEoxmTURZkiVGhwAOMY6zeffJrFYazZWcpJOeLM8fbF6D6bi9WFjwpDhntyOI+jIfdnNJQ4QjYZXy7ej/7/9XpFPQ+exKuV"
    "QVVt5Z3tXMv49sUTrSzo6gjvcrbx2sGVVUUQVeLYCmZDm+1yPt08cVDoesj4oiQnCZltSwZbfOZlSJxW3Is8GYX/VPWTzcmcCo503yITMEtyXc9mgRkCHWCa"
    "s+BcP5RjOVloui5H1kvk9aiNBbRIKZKA64pY8jYVIaXx0KgtxGKZ5OMx0lJNC/VFF5Tm5sZhUTQaWmr+1/iou9q8p5+wldXdcPMsRoZ0fkGwTIiTrWpcKhKc"
    "RcLpEnoR6Hhpa3fGEmJ2zyov3ys1Kng+IV5hS3WCISJGOF0eHVUoVhM5f2XkNUdxmAbqCwi/mIjKvhYFanaaMfJztmx0Yj1Lq/afkZwsDcdS0i6yfntDYn4r"
    "BJgwwKzKbUHCt72SQ6MIMLX47jz2t0bYUhTSEfIGpwMKtrLh3ggenuk7IkmqOCyrMQVuFoEXCStsvwmggY0ornjl/l7HcYhTISQrilY6hDuAwVxhC5Md8UHY"
    "ktoioYubWWIhO3RPxFc8hLIbp5dwWDIo8pkF4sYoEpJGHWd8QJqNDaFobtSVJK2yH3f6nfreZ2v7pBygDs3F1KpwwfXdoWlEag045INmMjBaG18L+TOePEyq"
    "VGUcYyCSQXFppTdekfCbYOCgoye4ApnddTBngWBftxQ0KOzjVAxW9s2TJ2UiNXYrLEUNpbM4hunPBR8VPZMjOOAvmVLBfSzg/yIdJKF4bWW0Z6K2cCZyHiaJ"
    "1FH9pidLe11vaT8qk1stjTv8rmAUrwTk4JzIKCKlURfy1+c4AJHpr3EsRom4nxwFl2x5eMebhhoqgU8jUmdPiRRSCU9kMne0tQoMMrxGk2s4yirGx0gmRQQl"
    "UQwmckvXS/ZhKlb0upbNOnsWIXqRZMnba+6U0T3TkoEEfxM++uSZLRLqGt+MkHApsLRlHsRirLAl/BQH3FlpAFypNqpHB8MTlBQUjOFbTxjqb6r5ewGJh1b2"
    "ait7twEUn8ed242p0z+KZFQHKCZD2OdstrYcrhNzpVMs7tOJ7Hg21iPL5O5SeSC5J4/EwHMIO29febpIJLKuObdF7MlqRgpjlCN0xFFSzeYqStJB4GCUxYpK"
    "fsXKGUKl8Jnzlt08Q/JSZjDZrECdniW1aM8zbnMOni8r7wEjGvEi6s49qINXOAojyBdp6xOxMUXNruIEUcbU3c3PMILHYPzToL5qlBVjNekbuRmdRdWTYzFv"
    "TMNkbFEIu2x7msGHeUYG1mOg0iteUgDOWQQWlLw0TEndMjjGmtpsd17A9Xl6+KQXY3QssCZKMtdEMghU5CGNQdSbqCACwefEbnuFhtbZQ3HaSJWov/GYE58E"
    "57NkNnxxW2Q5oYgL/ke/5lrobegMys1yyEE6imtdof2JK0LFkIFVQBAvRNF4N8/hbW7ZqGlIRdHyXCC0c9zQzm5E9OgIDZWW6QVaEurFL50FSTlRYbEz+17A"
    "cbEh1NxpuW2lCFi8T/V6O+tYlpDNDRCVFeghkDZh2ZAdFmiVWRBWxXD3a4FeKYX0i9AQ3wYH0lghQYVBXg8a/JXGQShxVs5KGvOdXV4rZNxcAEU5liptmiuD"
    "LVIAzQ4w5TaEhsRPxYYb6+4jYvApMAo8yL5di3eNPoWuLoeLpIKJ84yi7CIYwJqz8Ui0GgxhbGlN9Bk1Xd8tejU3UQr3sTk2wfk+cwvEOAwN50wg/iJC4bax"
    "qNwjpYjpz4AVo7LRjVKcdrBSajPanrL5gv6WTBt9OyKsxkFHR6Fv0qzHfx8FwqIFOnK4Ckg/xKwsND1XiiaNZUjoAP2sjUiFUUAZu8QiKbnkHZotM4+s58eE"
    "2WFHkvgi0pf2/Y6SdONDW1g64IRBeWRoJaHeJcV6XY+n459b7bufI5UhCxs31QBytiQX2z1kcYOCDV8AlV4ZJ00mLw+YiWXUMqCBjDdn0/ugnbvNZHy+Oyi2"
    "UooIgUvfq41rtG3SxwVrnkdJq/EX6uhRoRAA3XLdHkNRYRZk5rSa6VfuhCXQE8UBia5UYZA2ukbNDvwupQdIog+OfbetRl+rOW3lvLj4XKGwc2LU3SAygBxN"
    "qTh4dWhUq03paqONsbF3S8xCKlvZ+3IO27zNnAWxZB/S6HCSyy9VaUkKXWUSwegKzo+wWHDCcJ1HjVAJzBwkpYxbRHKnwpoZGVkd4ItyaXy+bkHvlTW5E7do"
    "iR0jAdtMivf8u0y/X8gnVzHjVGw4y2K3ey4bL1bd+FlUi5u1T2t+fOCc+zBJogXfnxSgmIuHuewy+msmJlwb2kUq324tP9dBn3f4ijZM3HSvzyZlUESBuMeC"
    "y3VXXOgZQIXw63jFYGZXPIc7DsZiNtesTmY0OODcC2KMicQUXj+uDihSFz9naFEQGmt3uyKmuPi4g3x1PgmzSr7QnNuxEfIOiiLAd659urkUbnflQQwE19DZ"
    "4QaqRHvtdmkY5+9ziJVviK81glQw7gtvl1IGS2Evq3O51qUfDqRiQpmuzc2ZD8EGXp4M3sIzydWROCtE2ShGj7ome3fuViN/VYAUyh3ZmdU5hMwQbQTmPBlQ"
    "ibnbXZWurp5DqbNOW+6Fjcc17lOJsEXyzchySU/zAyJMBqTuem4QZp035b7NaxO6CqXM93tKQr3nR2nB4V7nKBaBX0S4jCmj7M+ymds7nklpbx726G1wuuj5"
    "eydSAfbLhmZyyo0pECiC2SUvyM1zQjMuLatVkEgUvT2zTJOQi2ew4o54wTdbNZQFSozoaysaXK5JvHj7yb8J4AqxNQGj53d+GbG4Zl45wgoCgshtieYsbB1w"
    "XTaPAmWQio/irK22efWdUVaFEEgF5ABZ0Y3pseuV9eyAnZpZY8vdtzgVxc8gFI127FY8yLpYt1bEappFxD6jkj1w9ugIhdcztQi5Nz6NeKaMta1aFdn0TBa5"
    "CwHSy2C61euGRf6QUXQ91208oUO9dtLRyHojgm5rDKMDbHNc36ld0szmZ/JviNPsWVWw3A6hV+0S+qZ+LMx4XaDrvj03d2utzVl74KhjkgOc5lsh3TEgbgHj"
    "kojpAhlT+Km3wKNGqapiMi5iG3eL/VCIOU6VJEvuvDQwfohUZ4Hnsl5g9XsU/V0EFxkdao+YqQdAlh5at8aIX2xQy7vk1sCDrHfkb4Rf4mj2wD/36blDLsAW"
    "t+MO2hiuP8ciwVsjR06TWn+DoZSjWqFrwPMR6Turj0PnN6SRrFWF7XTtDAV8cG9tLUPBzuOCqBpndyqald9n7matcRqy+lL04jlmkVLqt9ytWZvS3uqY9Gv8"
    "4sXdEb5jbPiQ1VpuWFTGZmKxydnZrUMUQ+A/T6ykPMFR/EBr9fPewo+6S5BifxzKAnHj/MyXaascxrX6TClDA6RGK2jIivGBbyD0ihdBrGaat0VJEIKmd7nx"
    "3c2JDrE/9wWwjDI/lBYnLoPycRadrLLR07EFFX3QmCymqZzlPIJBtlQrj5V8Kyq1h1r3WyKDhu9hz5xD4sQt2SHRIemEpcGaFyTlUXt4coN0Kjp8ogmIOoit"
    "fkL9FmveIip9x2KH4owR5FiFNi3Cr0iKlqhHG26S6zOKck9+DV7FP69Cz8xsZdEXW2Rw7XZMnBfnvQCLjDyy8aQom94S+/m5RvYYcuEE6SzdRiEbqo+x9Prr"
    "XlfumZrmvXAqfocPoGgvpqwmE7qNcWJEgS5YjTCFJ0FYHYiY+FsaSSEW9HodurfS+LPgMod+bnaiKYdHc1UAKTTLkRcV4BwF+myFgGJXdKgDtSlHx8yelUQ3"
    "K2RrH3aEkEvbXDW80g++GtkkAxkvX3eb0PqGXMyrzVe7ecv22CbZM7vqjw7cYqSStOjyGbUjWGfa6IN0QVjDRU5S/XAEQWQ4kJBSh1SlVRY+GgeQi22xGRQj"
    "RpJxuo0o8zbIXkWltwnds5UBWiyQrsGydplzezLFm02PGycDuKkzM2njd2ZyY0OAF4gjJ27oc+UFCz6cVShKDqwUIkJ+LiPDg4xqtiNKJBzjAciHxiyBo3AW"
    "bcVkjagKpoWtg9ovUvCz91yJRccZCVqXEj2YV5LsEArSre+Q0eI5T4mgujdtrLeT6Z9mzLzTodb+4yWBtsHnoexgz/5FVaOlaqatWi1u5nsLkiPUAZyfSE1t"
    "pPDGiZbViboUA1/4YvS9oVHjdEV0iIliJ+Ci0s8FyZ/blOe1sBq1d8RGc59wvcnYXQvdu+I2Cr+RkBgGVJs9UwiOse0tOwhWsKDnbhng12TNin4dVr+244Yu"
    "B8xe+RCZELRAXpIy4Y4Vq7Oy4nfJL2Mr9eTNOydUxnLcs1K07IiaO+7G7Dr7fF+RCIDOYezlEUA+5M3EN0WqLs6ydpKIeV91FxVRaROgF6sPBCmznlxlmRDg"
    "YqalJ2GlBC6vRTFv94TQIBnPyReJRxq4dnx4sngU3S88Uxry5HmAXqrIKjI3/r7La6zzHKLcGyrpeIWE+uIw8GBaKRkBYscvnyQsm/Vh1BmzE5l45EPyy8Rq"
    "SsIt7KurIsC9yAK5xmvbnI/l4eDV5s2nsVz/pUHiYr8cPkwhz4LVmTzjux9SAPs9bwjPFrQMX5IaEzpoKcJDC6Wcg+Bl7aM+KMjfYa7HfFZbHy35otmV2Hj0"
    "nqIJQ7xBcibu7UKtYwuiPyr/tLnWUaMJOl45M6ssNs2ya/SlHJHYNRe6n/DInLVyTisOFFfIVohm+OXQxOcA98y/+08FSaVOs5KWDifNeqxzG0GdZGub8Ynb"
    "7QgpzDYJq9BJLRz3RuT8OxCordXJd5LgpeVv4UJajnT+Dsq1uHYc+HeiDMqZQXxrFfq0ujXV2I/UMuBWW8W453AEg+d0fiZ0Ga3HgO/l26+XGMprfT3rB9Sh"
    "PijViGKNWgYhJDYxjkLSF0Jwjs3Y734j2KyVpTTUCXcJi38gKQxQ0XIxrY0OIdNCOI51nT1cYFfPiwtPxSSp1hOBdD2a/id7sF7++vlZ5JNsUJusMMoeggFI"
    "zqqbt825mPaTMgfs3bMIxXtBSdbHTfAcroty7P83jmyzvUiiFDohaVQrz0FSAvOOkleGzmU+p6BBbnFyHhSbWiK7FLNW5hbYMPZCJMOF8FMdcOOw75t6Bb9B"
    "51QRRlV++Xc6eJqhSrWJDaMrDF7IujChdcdBwQW/KkzKGZ8c5M3fVDEUEcwUqbwhnOJ1oc5TiYjVe1bVeoHe2eKuucw/EcOL8jc568EOyTyJeHMjYZbLiFIf"
    "G3ksYWV1sIwQLb9p/oOxVBhffzy3iOniRq/Ph7ozeu4wncl69PwBSa8ii1fjL8rwMfaDMeFMuNr6ixnRKjix5Cj4/jDUVaKCZyR/3m7qsCeMTBnztKZw9BI3"
    "Ls6+KgZP567sHVSgySqBd0HKrCJ99p+9iJZ0TWEf9pvnY1mrmpF9gC8qZ9/ay8tXR1Kq5YA2l2fDfc8T6o1LFEjdbmnp2eRgzqnnc1MUUpvaagLwToVT7FGx"
    "Glf3htRqT6Lxc8fxijLc7B+VolHepH0rnCtXL62/Y6nxrSBJVmj4W+zaqssrzs9tx26Lzjtbli8qbdtbjGuhEBo+tEALUge++OyrvlevqRLQ1LJfUZUt2PB3"
    "Ku7KJvi+xE7FZN/IjptZZeasrBUM3dQogjtpSzB9hlxX9JS/i8at1mNJmta/W4ymopgPx2rsKMZo227p7uc2CN6OHL593leW2BicuBgB1YRwLsQi9M7e9N93"
    "BPxifH239hXBUbOuVfO/lboXY95RtKAaY4OSdJIvy2plLA/hg2/lod84oftm5DDJ8XYbO786ZGNxDr356W+LTzRDs8uOIa3/gscRZksDv3eQjr+NgixIpirp"
    "F6fR5XGzBFJSVFKm8b8Fz2hHc4b9eJrdVYiEHXGaERWWzOU89G9BSLUJzO33Kb4fFk3tSiLd33RAcorF5ctq2cWiCx+IHZTOwW0JgQqIhdfuM+as2yUYjBjl"
    "Qs5ilGx/7V0THKvHuiAdK7fCJfa7/7uZo4iEPozbnK6M/UnM8RDOX/FmZgHqRPKtAwb5kLkN8Htzu9N7OIaTyLuicCuxJ1kUz/iZt7I8zGo5Auzei6dMCLq7"
    "77rkN2kjRdEynoMN/0FjsvqMg0vjN70XqpcAuRy2AOZsbqR0Ei0gyQIfnzODyIco93L726fADeyO84AKh9jUXE5js/kazsE61/6fPX/03qdnf3jV2M1YZt0g"
    "J8RETcsEMcK9oFI47dZTXjfBiGz4myaddxBL/ntuWvIeutvvAOGIoBJVIDVyvPQ397XYV5xRPfg61pUXS4tS2DirTd5V1rRa6bfFyc3M27dlGFeVNxX1KndT"
    "O+RccFyjp4PVKm45fjvYpapEt7bJrKuN4f1zrzjXIfE3KPqDQ7Sq08/Xa4sKpTfW5+CJEuPckjIj8LotnhHUYeifH6CkcJnGvIt6NmdXw+U9UifEUo1QB5dK"
    "WcIo08aJ8z2AcyaGbAsOSzzNm3UorTc1IdiRTaJzvPF5c4Jy998torjRBh0cVU2s6c3ecJy1wl7ekR+yvm+Onj7Vs/oOzGoJuEnNYDkuDeP7oUVaueev9xQo"
    "Od68b4t9ieDrKBx393vyMnMEzp24WaNpH6d7qmO5dwKeB/X9er1DmtOic1gRtv4mRFN/pvwi/x8QJYS9AbQ6oQAAAABJRU5ErkJggg=="
)
SPLASH_MIN_VISIBLE_MS = 1800


# Hidden SMN-Systems easter egg.
# Six generated animation poses are embedded directly in the notebook.
EASTER_EGG_FRAMES_B64 = [
    (
        "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"
        "rkJggg=="
    ),
    (
        "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"

        dialog.title(
            self._tr("Sesión experimental / metadatos")
        )
        dialog.geometry(
            "760x470"
        )
        dialog.minsize(
            680,
            430
        )
        dialog.transient(
            self
        )

        outer = ttk.Frame(
            dialog,
            padding=16
        )
        outer.pack(
            fill="both",
            expand=True
        )

        ttk.Label(
            outer,
            text="Metadatos de la sesión experimental",
            style="Section.TLabel"
        ).grid(
            row=0,
            column=0,
            columnspan=4,
            sticky="w"
        )

        ttk.Label(
            outer,
            text=(
                "Todos los campos son opcionales. Se aplicarán a las "
                "ejecuciones realizadas mientras esta sesión permanezca "
                "activa y quedarán guardados en el JSON de configuración."
            ),
            style="Muted.TLabel",
            wraplength=700
        ).grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(3, 14)
        )

        vars_local = {
            key: tk.StringVar(
                value=str(
                    self.session_metadata.get(
                        key,
                        ""
                    )
                )
            )
            for key in (
                "session_id",
                "sample_id",
                "preparation",
                "eye_retina",
                "group_treatment",
                "operator",
            )
        }

        labels = (
            (
                "ID de sesión",
                "session_id",
                2,
                0
            ),
            (
                "ID de muestra",
                "sample_id",
                2,
                2
            ),
            (
                "Preparación",
                "preparation",
                3,
                0
            ),
            (
                "Ojo / retina",
                "eye_retina",
                3,
                2
            ),
            (
                "Grupo / tratamiento",
                "group_treatment",
                4,
                0
            ),
            (
                "Operador",
                "operator",
                4,
                2
            ),
        )

        for (
            label_text,
            key,
            row,
            col,
        ) in labels:
            ttk.Label(
                outer,
                text=self._tr(label_text + ":")
            ).grid(
                row=row,
                column=col,
                sticky="w",
                padx=(
                    0,
                    8
                ),
                pady=6
            )

            ttk.Entry(
                outer,
                textvariable=vars_local[
                    key
                ],
                width=24
            ).grid(
                row=row,
                column=col + 1,
                sticky="ew",
                padx=(
                    0,
                    18
                ),
                pady=6
            )

        ttk.Label(
            outer,
            text="Notas:"
        ).grid(
            row=5,
            column=0,
            sticky="nw",
            pady=(10, 4)
        )

        notes = tk.Text(
            outer,
            height=7,
            wrap="word",
            bd=1,
            relief="solid"
        )
        notes.grid(
            row=6,
            column=0,
            columnspan=4,
            sticky="nsew",
            pady=(0, 12)
        )
        notes.insert(
            "1.0",
            str(
                self.session_metadata.get(
                    "notes",
                    ""
                )
            )
        )

        button_row = ttk.Frame(
            outer
        )
        button_row.grid(
            row=7,
            column=0,
            columnspan=4,
            sticky="e"
        )

        def close_dialog():
            try:
                dialog.destroy()
            finally:
                self.session_dialog = None

        def clear_dialog():
            for variable in (
                vars_local.values()
            ):
                variable.set("")
            notes.delete(
                "1.0",
                "end"
            )

        def save_dialog():
            defined_at = str(
                self.session_metadata.get(
                    "defined_at",
                    ""
                )
            ).strip()

            values = {
                key:
                    variable.get().strip()
                for (
                    key,
                    variable
                ) in vars_local.items()
            }
            values[
                "notes"
            ] = notes.get(
                "1.0",
                "end-1c"
            ).strip()

            any_content = any(
                values.values()
            )

            if (
                any_content
                and not defined_at
            ):
                defined_at = (
                    datetime.now().isoformat(
                        timespec="seconds"
                    )
                )

            if not any_content:
                defined_at = ""

            values[
                "defined_at"
            ] = defined_at

            self.session_metadata = values
            self._update_session_summary()

            if any_content:
                self.status.set(
                    "Metadatos de sesión actualizados."
                )
            else:
                self.status.set(
                    "Sesión sin metadatos activos."
                )

            close_dialog()

        ttk.Button(
            button_row,
            text="Limpiar campos",
            command=clear_dialog,
            style="Secondary.TButton"
        ).pack(
            side="left",
            padx=(0, 8)
        )

        ttk.Button(
            button_row,
            text="Cancelar",
            command=close_dialog,
            style="Secondary.TButton"
        ).pack(
            side="left",
            padx=(0, 8)
        )

        ttk.Button(
            button_row,
            text="Guardar metadatos",
            command=save_dialog,
            style="Accent.TButton"
        ).pack(
            side="left"
        )

        for col in (
            1,
            3
        ):
            outer.columnconfigure(
                col,
                weight=1
            )
        outer.rowconfigure(
            6,
            weight=1
        )

        dialog.protocol(
            "WM_DELETE_WINDOW",
            close_dialog
        )

        dialog.grab_set()
        dialog.focus_force()

    def _set_last_run_folder(
        self,
        folder
    ):
        folder = str(
            folder
            or ""
        ).strip()

        if not folder:
            return

        self.last_run_folder = folder


    def _open_logs_folder(self):
        path = Path(
            LOG_DIR
        )

        try:
            path.mkdir(
                parents=True,
                exist_ok=True
            )
        except Exception as exc:
            messagebox.showerror(
                "Abrir logs",
                (
                    "No se ha podido preparar la carpeta de logs.\n\n"
                    f"{exc}"
                ),
                parent=self
            )
            return

        try:
            if os.name == "nt":
                os.startfile(
                    str(path)
                )
            elif sys.platform == "darwin":
                subprocess.Popen(
                    [
                        "open",
                        str(path)
                    ]
                )
            else:
                subprocess.Popen(
                    [
                        "xdg-open",
                        str(path)
                    ]
                )
        except Exception as exc:
            messagebox.showerror(
                "Abrir logs",
                (
                    "No se ha podido abrir la carpeta de logs.\n\n"
                    f"{exc}"
                ),
                parent=self
            )


    def _open_manual(self):
        """Open the offline v2.0 user manual without requiring Internet."""
        candidates = []

        try:
            if getattr(sys, "frozen", False):
                candidates.append(
                    Path(sys.executable).resolve().parent / MANUAL_FILENAME
                )
        except Exception:
            pass

        try:
            if "__file__" in globals():
                candidates.append(
                    Path(__file__).resolve().parent / MANUAL_FILENAME
                )
        except Exception:
            pass

        try:
            candidates.append(Path.cwd() / MANUAL_FILENAME)
        except Exception:
            pass

        # Future installer builds may place documentation in a docs subfolder.
        expanded = []
        for candidate in candidates:
            expanded.append(candidate)
            expanded.append(candidate.parent / "docs" / MANUAL_FILENAME)

        manual_path = next(
            (p for p in expanded if p.exists() and p.is_file()),
            None
        )

        if manual_path is None:
            locations = "\n".join(
                f"• {p}" for p in expanded[:6]
            )
            messagebox.showwarning(
                "Manual / Ayuda",
                (
                    "No se encuentra el manual offline.\n\n"
                    f"Archivo esperado: {MANUAL_FILENAME}\n\n"
                    "Coloca el PDF junto al programa (o en una carpeta "
                    "«docs» junto al programa) y vuelve a intentarlo.\n\n"
                    f"Ubicaciones comprobadas:\n{locations}"
                ),
                parent=self
            )
            return

        try:
            if os.name == "nt":
                os.startfile(str(manual_path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(manual_path)])
            else:
                subprocess.Popen(["xdg-open", str(manual_path)])
            self.status.set(
                f"Manual abierto: {manual_path.name}"
            )
        except Exception as exc:
            messagebox.showerror(
                "Manual / Ayuda",
                (
                    "No se ha podido abrir el manual.\n\n"
                    f"{manual_path}\n\n{exc}"
                ),
                parent=self
            )


    def _get_operator_screen_for_splash(self):
        """
        Choose a monitor different from the saved experimental monitor.
        With one screen, fall back to that screen.
        """
        try:
            screens = detect_screens()
            if not screens:
                return None

            default_exp = (
                1 if len(screens) > 1 else 0
            )
            exp_idx = int(
                self.saved.get(
                    "screen_index",
                    default_exp
                )
            )
            if not (
                0 <= exp_idx < len(screens)
            ):
                exp_idx = default_exp

            for screen in screens:
                if screen["index"] != exp_idx:
                    return screen

            return screens[0]
        except Exception:
            return None

    def _create_splash(self):
        try:
            splash = tk.Toplevel(self)
            self.splash = splash

            splash.overrideredirect(True)
            splash.configure(bg="#FFFFFF")

            # Keep it above normal desktop windows, but it is explicitly
            # positioned on the operator monitor, never the experimental one.
            try:
                splash.attributes(
                    "-topmost", True
                )
            except Exception:
                pass

            outer = tk.Frame(
                splash,
                bg="#FFFFFF",
                highlightbackground="#C9D5DB",
                highlightthickness=1,
                bd=0
            )
            outer.pack(
                fill="both",
                expand=True
            )

            # Tk PhotoImage can read the embedded PNG directly from base64.
            full_image = tk.PhotoImage(
                data=SPLASH_IMAGE_B64
            )
            self.splash_image = full_image

            image_label = tk.Label(
                outer,
                image=self.splash_image,
                bg="#FFFFFF",
                bd=0
            )
            image_label.pack(
                padx=18,
                pady=(16, 6)
            )

            tk.Label(
                outer,
                text="MEA Visual Stimuli",
                bg="#FFFFFF",
                fg="#1F2D33",
                font=(
                    "Segoe UI Semibold",
                    15
                )
            ).pack(
                pady=(1, 0)
            )

            tk.Label(
                outer,
                text=f"Versión {APP_VERSION}",
                bg="#FFFFFF",
                fg="#677983",
                font=("Segoe UI", 9)
            ).pack(
                pady=(1, 10)
            )

            status_label = tk.Label(
                outer,
                textvariable=self.splash_status_var,
                bg="#FFFFFF",
                fg="#255A73",
                font=(
                    "Segoe UI Semibold",
                    10
                )
            )
            status_label.pack(
                padx=20,
                pady=(0, 3)
            )
            self.splash_status_label = (
                status_label
            )

            detail_label = tk.Label(
                outer,
                textvariable=self.splash_detail_var,
                bg="#FFFFFF",
                fg="#677983",
                font=("Segoe UI", 9),
                wraplength=500,
                justify="center"
            )
            detail_label.pack(
                padx=24,
                pady=(0, 9)
            )
            self.splash_detail_label = (
                detail_label
            )

            self.splash_progress = ttk.Progressbar(
                outer,
                orient="horizontal",
                mode="indeterminate",
                length=360
            )
            self.splash_progress.pack(
                pady=(0, 13)
            )
            self.splash_progress.start(12)

            self.splash_open_btn = ttk.Button(
                outer,
                text="Abrir interfaz",
                command=self._force_open_interface,
                style="Secondary.TButton"
            )

            splash.update_idletasks()

            sw = splash.winfo_reqwidth()
            sh = splash.winfo_reqheight()

            operator_screen = (
                self._get_operator_screen_for_splash()
            )

            if operator_screen is not None:
                x = int(
                    operator_screen["x"]
                    + (
                        operator_screen["width"]
                        - sw
                    ) / 2
                )
                y = int(
                    operator_screen["y"]
                    + (
                        operator_screen["height"]
                        - sh
                    ) / 2
                )
            else:
                x = int(
                    (
                        splash.winfo_screenwidth()
                        - sw
                    ) / 2
                )
                y = int(
                    (
                        splash.winfo_screenheight()
                        - sh
                    ) / 2
                )

            splash.geometry(
                f"{sw}x{sh}{x:+d}{y:+d}"
            )
            splash.update_idletasks()
            splash.update()

        except Exception:
            # A splash failure must never prevent the experiment software
            # from opening. Continue with the normal interface.
            self.splash = None
            self.splash_image = None
            self.after(
                1,
                self._force_open_interface
            )

    def _splash_update(
        self, status_text, detail_text=""
    ):
        if (
            self.splash is None
            or not self.splash.winfo_exists()
        ):
            return

        try:
            self.splash_status_var.set(
                str(status_text)
            )
            self.splash_detail_var.set(
                str(detail_text)
            )
            self.splash.update_idletasks()
        except Exception:
            pass

    def _splash_show_error(
        self, message
    ):
        self.splash_error = True

        if (
            self.splash is None
            or not self.splash.winfo_exists()
        ):
            self._force_open_interface()
            return

        try:
            self.splash_progress.stop()
            self.splash_progress.pack_forget()

            self.splash_status_label.configure(
                fg="#B65353"
            )

            self.splash_status_var.set(
                "No se pudo completar la inicialización"
            )
            self.splash_detail_var.set(
                str(message)
            )

            if not self.splash_open_btn.winfo_manager():
                self.splash_open_btn.pack(
                    pady=(0, 14)
                )
        except Exception:
            self._force_open_interface()

    def _request_splash_close(
        self, hz=None
    ):
        if self.splash_ready_requested:
            return

        self.splash_ready_requested = True

        if hz is not None:
            self._splash_update(
                "Sistema preparado",
                (
                    "Pantalla experimental estable · "
                    f"{float(hz):.3f} Hz · espera segura"
                )
            )
        else:
            self._splash_update(
                "Sistema preparado",
                "Abriendo interfaz del operador"
            )

        elapsed_ms = int(
            (
                time.perf_counter()
                - self.splash_started
            ) * 1000
        )
        wait_ms = max(
            0,
            SPLASH_MIN_VISIBLE_MS
            - elapsed_ms
        )

        self.after(
            wait_ms,
            self._finish_splash
        )

    def _finish_splash(self):
        if self.splash_error:
            return

        try:
            if self.splash is not None:
                try:
                    self.splash_progress.stop()
                except Exception:
                    pass
                try:
                    self.splash.destroy()
                except Exception:
                    pass
        finally:
            self.splash = None
            self.deiconify()
            try:
                if int(self.winfo_screenheight()) <= 900:
                    self.state("zoomed")
            except Exception:
                pass
            try:
                self.lift()
                self.focus_force()
            except Exception:
                pass

    def _force_open_interface(self):
        self.splash_error = False

        try:
            if self.splash is not None:
                try:
                    self.splash_progress.stop()
                except Exception:
                    pass
                try:
                    self.splash.destroy()
                except Exception:
                    pass
        finally:
            self.splash = None
            self.deiconify()
            try:
                if int(self.winfo_screenheight()) <= 900:
                    self.state("zoomed")
            except Exception:
                pass
            try:
                self.lift()
                self.focus_force()
            except Exception:
                pass

    def _configure_styles(self):
        self.palette = {
            "bg": "#F3F6F8",
            "panel": "#FFFFFF",
            "panel_alt": "#F8FAFB",
            "border": "#D9E2E7",
            "text": "#1F2D33",
            "muted": "#677983",
            "accent": "#2F6F8F",
            "accent_dark": "#255A73",
            "success": "#3B7D62",
            "danger": "#B65353",
            "danger_dark": "#914141",
            "preview_bg": "#EEF2F4",
            "gray50": "#808080",
        }

        self.configure(bg=self.palette["bg"])
        self.option_add("*Font", ("Segoe UI", 10))

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "TFrame",
            background=self.palette["panel"]
        )
        style.configure(
            "TLabel",
            background=self.palette["panel"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 11)
        )
        style.configure(
            "TLabelframe",
            background=self.palette["panel"],
            bordercolor=self.palette["border"],
            relief="solid",
            borderwidth=1
        )
        style.configure(
            "TLabelframe.Label",
            background=self.palette["panel"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 11)
        )

        style.configure(
            "App.TFrame",
            background=self.palette["bg"]
        )
        style.configure(
            "Card.TFrame",
            background=self.palette["panel"]
        )
        style.configure(
            "TabBody.TFrame",
            background=self.palette["panel"]
        )

        style.configure(
            "Title.TLabel",
            background=self.palette["bg"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 24)
        )
        style.configure(
            "Subtitle.TLabel",
            background=self.palette["bg"],
            foreground=self.palette["muted"],
            font=("Segoe UI Semibold", 11)
        )
        style.configure(
            "Section.TLabel",
            background=self.palette["panel"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 10)
        )
        style.configure(
            "Muted.TLabel",
            background=self.palette["panel"],
            foreground=self.palette["muted"],
            font=("Segoe UI", 9)
        )
        style.configure(
            "Status.TLabel",
            background=self.palette["panel"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 10)
        )
        style.configure(
            "Badge.TLabel",
            background="#E8F2F6",
            foreground=self.palette["accent_dark"],
            padding=(10, 5),
            font=("Segoe UI Semibold", 10)
        )
        style.configure(
            "ProtocolDuration.TLabel",
            background=self.palette["panel"],
            foreground=self.palette["accent_dark"],
            font=("Segoe UI Semibold", 18)
        )
        style.configure(
            "ProtocolDurationCaption.TLabel",
            background=self.palette["panel"],
            foreground=self.palette["muted"],
            font=("Segoe UI", 9)
        )

        style.configure(
            "Card.TLabelframe",
            background=self.palette["panel"],
            bordercolor=self.palette["border"],
            relief="solid",
            borderwidth=1
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self.palette["panel"],
            foreground=self.palette["text"],
            font=("Segoe UI Semibold", 10)
        )

        style.configure(
            "TNotebook",
            background=self.palette["bg"],
            borderwidth=0
        )
        style.configure(
            "TNotebook.Tab",
            background="#E8EEF1",
            foreground=self.palette["muted"],
            padding=(13, 7),
            font=("Segoe UI Semibold", 10),
            borderwidth=0
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", self.palette["panel"]),
                ("active", "#F4F7F9"),
            ],
            foreground=[
                ("selected", self.palette["accent_dark"]),
                ("active", self.palette["text"]),
            ]
        )

        style.configure(
            "Accent.TButton",
            background=self.palette["accent"],
            foreground="white",
            padding=(15, 8),
            borderwidth=0,
            font=("Segoe UI Semibold", 10)
        )
        style.map(
            "Accent.TButton",
            background=[
                ("active", self.palette["accent_dark"]),
                ("disabled", "#A7BBC5"),
            ],
            foreground=[
                ("disabled", "#EDF2F4"),
            ]
        )

        style.configure(
            "Danger.TButton",
            background=self.palette["danger"],
            foreground="white",
            padding=(13, 8),
            borderwidth=0,
            font=("Segoe UI Semibold", 9)
        )
        style.map(
            "Danger.TButton",
            background=[
                ("active", self.palette["danger_dark"]),
                ("disabled", "#C9B0B0"),
            ],
            foreground=[
                ("disabled", "#F3EDED"),
            ]
        )
        self.protocol_pause_color = tk.StringVar(
            value="Fondo de adaptación"
        )
        self.protocol_duration_var = tk.StringVar(
            value="00:00:00"
        )
        self.protocol_duration_detail_var = tk.StringVar(
            value="1 vuelta: 00:00:00 · ×1 repetición"
        )
        self.protocol_structure_var = tk.StringVar(
            value="0 pasos · 0 estímulos · 0 pausas"
        )
        self.protocol_builder_status = tk.StringVar(
            value=(
                "Crea un protocolo nuevo o carga uno guardado. "
                "Configura un estímulo en su pestaña y pulsa "
                "«Añadir al protocolo»."
            )
        )

        body = ttk.Frame(
            self.tab_protocols,
            style="TabBody.TFrame"
        )
        body.pack(
            fill="both",
            expand=True,
            padx=10,
            pady=10
        )
        body.columnconfigure(0, weight=0, minsize=220)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # --------------------------------------------------
        # Saved protocol library - permanently visible.
        # The list absorbs all spare vertical room while the
        # action buttons stay reachable at the bottom.
        # --------------------------------------------------
        library = ttk.LabelFrame(
            body,
            text="Protocolos guardados",
            style="Card.TLabelframe"
        )
        library.grid(
            row=0, column=0,
            sticky="nsew",
            padx=(0, 7)
        )
        library.columnconfigure(0, weight=1)
        library.rowconfigure(0, weight=1, minsize=90)

        list_host = ttk.Frame(
            library,
            style="Card.TFrame"
        )
        list_host.grid(
            row=0, column=0,
            sticky="nsew",
            padx=7,
            pady=7
        )
        list_host.columnconfigure(0, weight=1)
        list_host.rowconfigure(0, weight=1)

        self.protocol_listbox = tk.Listbox(
            list_host,
            width=24,
            height=10,
            exportselection=False,
            activestyle="none",
            font=("Segoe UI", 10),
            bg="#FBFCFD",
            fg=self.palette["text"],
            selectbackground="#D9E8EF",
            selectforeground=self.palette["text"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            bd=0
        )
        self.protocol_listbox.grid(
            row=0, column=0,
            sticky="nsew"
        )

        protocol_scroll = ttk.Scrollbar(
            list_host,
            orient="vertical",
            command=self.protocol_listbox.yview
        )
        protocol_scroll.grid(
            row=0, column=1,
            sticky="ns"
        )
        self.protocol_listbox.configure(
            yscrollcommand=protocol_scroll.set
        )
        self.protocol_listbox.bind(
            "<Double-Button-1>",
            lambda event:
                self._load_selected_protocol()
        )

        lib_buttons = ttk.Frame(
            library,
            style="Card.TFrame"
        )
        lib_buttons.grid(
            row=1, column=0,
            sticky="ew",
            padx=7,
            pady=(0, 7)
        )

        ttk.Button(
            lib_buttons,
            text="Nuevo",
            command=self._new_protocol,
            style="Secondary.TButton"
        ).grid(
            row=0, column=0,
            sticky="ew",
            padx=(0, 3),
            pady=2
        )
        ttk.Button(
            lib_buttons,
            text="Cargar",
            command=self._load_selected_protocol,
            style="Secondary.TButton"
        ).grid(
            row=0, column=1,
            sticky="ew",
            padx=(3, 0),
            pady=2
        )
        ttk.Button(
            lib_buttons,
            text="Duplicar",
            command=self._duplicate_selected_protocol,
            style="Secondary.TButton"
        ).grid(
            row=1, column=0,
            sticky="ew",
            padx=(0, 3),
            pady=2
        )
        ttk.Button(
            lib_buttons,
            text="Eliminar",
            command=self._delete_selected_protocol,
            style="Danger.TButton"
        ).grid(
            row=1, column=1,
            sticky="ew",
            padx=(3, 0),
            pady=2
        )

        self.protocol_export_btn = ttk.Button(
            lib_buttons,
            text="Exportar…",
            command=self._export_selected_protocol,
            style="Secondary.TButton"
        )
        self.protocol_export_btn.grid(
            row=2, column=0,
            sticky="ew",
            padx=(0, 3),
            pady=(4, 2)
        )

        self.protocol_import_btn = ttk.Button(
            lib_buttons,
            text="Importar…",
            command=self._import_protocol_file,
            style="Secondary.TButton"
        )
        self.protocol_import_btn.grid(
            row=2, column=1,
            sticky="ew",
            padx=(3, 0),
            pady=(4, 2)
        )

        lib_buttons.columnconfigure(0, weight=1)
        lib_buttons.columnconfigure(1, weight=1)

        # --------------------------------------------------
        # Protocol editor.  v1.26 deliberately spends width
        # to recover vertical room: compact header, a large
        # central table and a narrow action rail on the right.
        # --------------------------------------------------
        editor = ttk.LabelFrame(
            body,
            text="Editor del protocolo",
            style="Card.TLabelframe"
        )
        editor.grid(
            row=0, column=1,
            sticky="nsew",
            padx=(7, 0)
        )
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(1, weight=1, minsize=150)

        header = ttk.Frame(
            editor,
            style="Card.TFrame"
        )
        header.grid(
            row=0, column=0,
            sticky="ew",
            padx=9,
            pady=(8, 5)
        )
        header.columnconfigure(1, weight=1)

        ttk.Label(
            header,
            text="Nombre:"
        ).grid(
            row=0, column=0,
            sticky="w"
        )
        name_entry = ttk.Entry(
            header,
            textvariable=self.protocol_name_var
        )
        name_entry.grid(
            row=0, column=1,
            sticky="ew",
            padx=(6, 12)
        )
        name_entry.bind(
            "<KeyRelease>",
            lambda event:
                self._protocol_mark_dirty()
        )

        ttk.Label(
            header,
            text="Repetir:"
        ).grid(
            row=0, column=2,
            sticky="w"
        )
        reps_entry = ttk.Entry(
            header,
            textvariable=self.protocol_reps_var,
            width=6
        )
        reps_entry.grid(
            row=0, column=3,
            sticky="w",
            padx=(6, 4)
        )
        reps_entry.bind(
            "<KeyRelease>",
            lambda event:
                self._protocol_mark_dirty()
        )
        ttk.Label(
            header,
            text="veces"
        ).grid(
            row=0, column=4,
            sticky="w"
        )

        summary_strip = ttk.Frame(
            header,
            style="Card.TFrame"
        )
        summary_strip.grid(
            row=1, column=0,
            columnspan=5,
            sticky="ew",
            pady=(5, 0)
        )

        ttk.Label(
            summary_strip,
            text="Duración:",
            style="ProtocolDurationCaption.TLabel"
        ).pack(side="left")
        ttk.Label(
            summary_strip,
            textvariable=self.protocol_duration_var,
            style="ProtocolDuration.TLabel"
        ).pack(side="left", padx=(5, 12))
        ttk.Label(
            summary_strip,
            textvariable=self.protocol_duration_detail_var,
            style="Section.TLabel"
        ).pack(side="left")
        ttk.Label(
            summary_strip,
            text="  ·  ",
            style="Muted.TLabel"
        ).pack(side="left")
        ttk.Label(
            summary_strip,
            textvariable=self.protocol_structure_var,
            style="Muted.TLabel"
        ).pack(side="left")

        # Main workspace = large step table + fixed-width action rail.
        workspace = ttk.Frame(
            editor,
            style="Card.TFrame"
        )
        workspace.grid(
            row=1, column=0,
            sticky="nsew",
            padx=9,
            pady=(2, 5)
        )
        workspace.columnconfigure(0, weight=1)
        workspace.columnconfigure(1, weight=0, minsize=136)
        workspace.rowconfigure(0, weight=1)

        tree_host = ttk.Frame(
            workspace,
            style="Card.TFrame"
        )
        tree_host.grid(
            row=0, column=0,
            sticky="nsew",
            padx=(0, 7)
        )
        tree_host.columnconfigure(0, weight=1)
        tree_host.rowconfigure(0, weight=1)

        columns = (
            "number",
            "type",
            "summary",
            "duration"
        )
        self.protocol_tree = ttk.Treeview(
            tree_host,
            columns=columns,
            show="headings",
            height=14,
            selectmode="browse"
        )
        self.protocol_tree.heading(
            "number", text="#"
        )
        self.protocol_tree.heading(
            "type", text=self._tr("Estímulo")
        )
        self.protocol_tree.heading(
            "summary", text=self._tr("Configuración")
        )
        self.protocol_tree.heading(
            "duration", text=self._tr("Duración")
        )

        self.protocol_tree.column(
            "number",
            width=36,
            minwidth=36,
            stretch=False,
            anchor="center"
        )
        self.protocol_tree.column(
            "type",
            width=112,
            minwidth=88,
            stretch=False
        )
        self.protocol_tree.column(
            "summary",
            width=430,
            minwidth=220,
            stretch=True
        )
        self.protocol_tree.column(
            "duration",
            width=82,
            minwidth=74,
            stretch=False,
            anchor="e"
        )

        tree_scroll_y = ttk.Scrollbar(
            tree_host,
            orient="vertical",
            command=self.protocol_tree.yview
        )
        tree_scroll_x = ttk.Scrollbar(
            tree_host,
            orient="horizontal",
            command=self.protocol_tree.xview
        )
        self.protocol_tree.configure(
            yscrollcommand=tree_scroll_y.set,
            xscrollcommand=tree_scroll_x.set
        )
        self.protocol_tree.grid(
            row=0, column=0,
            sticky="nsew"
        )
        tree_scroll_y.grid(
            row=0, column=1,
            sticky="ns"
        )
        tree_scroll_x.grid(
            row=1, column=0,
            sticky="ew"
        )

        # Right-side protocol actions remain permanently available, but the
        # rail itself can scroll vertically on short displays.  This preserves
        # the useful saved-protocol library and the large central step table
        # without allowing "Añadir pausa" to fall below the visible area.
        action_shell = ttk.Frame(
            workspace,
            style="Card.TFrame"
        )
        action_shell.grid(
            row=0, column=1,
            sticky="nsew"
        )
        action_shell.columnconfigure(0, weight=1)
        action_shell.rowconfigure(0, weight=1)

        action_canvas = tk.Canvas(
            action_shell,
            highlightthickness=0,
            bd=0,
            bg=self.palette["panel"]
        )
        action_scroll = ttk.Scrollbar(
            action_shell,
            orient="vertical",
            command=action_canvas.yview
        )
        action_canvas.configure(
            yscrollcommand=action_scroll.set
        )
        action_canvas.grid(
            row=0, column=0,
            sticky="nsew"
        )
        action_scroll.grid(
            row=0, column=1,
            sticky="ns"
        )

        action_rail = ttk.Frame(
            action_canvas,
            style="Card.TFrame"
        )
        # Compact two-column action grid. This keeps the useful permanent
        # right-hand rail while freeing enough vertical space for the pause
        # controls on shorter operator displays.
        action_rail.columnconfigure(0, weight=1, uniform="protocol_actions")
        action_rail.columnconfigure(1, weight=1, uniform="protocol_actions")
        action_window = action_canvas.create_window(
            (0, 0),
            window=action_rail,
            anchor="nw"
        )

        def _sync_protocol_action_scroll(event=None):
            try:
                bbox = action_canvas.bbox("all")
                if bbox:
                    action_canvas.configure(
                        scrollregion=bbox
                    )
            except Exception:
                pass

        def _fit_protocol_action_width(event):
            try:
                action_canvas.itemconfigure(
                    action_window,
                    width=max(1, int(event.width))
                )
            except Exception:
                pass

        action_rail.bind(
            "<Configure>",
            _sync_protocol_action_scroll,
            add="+"
        )
        action_canvas.bind(
            "<Configure>",
            _fit_protocol_action_width,
            add="+"
        )

        ttk.Label(
            action_rail,
            text="Paso seleccionado",
            style="Section.TLabel"
        ).grid(
            row=0, column=0, columnspan=2,
            sticky="w",
            pady=(0, 4)
        )

        # Two columns instead of one long vertical stack. The existing
        # button styles are retained so normal and red-mode theming remain
        # unchanged.
        action_buttons = (
            ("↑ Subir", lambda: self._move_protocol_step(-1), "Secondary.TButton"),
            ("↓ Bajar", lambda: self._move_protocol_step(1), "Secondary.TButton"),
            ("Editar", self._edit_protocol_step, "Secondary.TButton"),
            ("Duplicar", self._duplicate_protocol_step, "Secondary.TButton"),
        )
        for idx, (text, command, style_name) in enumerate(action_buttons):
            ttk.Button(
                action_rail,
                text=text,
                command=command,
                style=style_name
            ).grid(
                row=1 + idx // 2,
                column=idx % 2,
                sticky="ew",
                padx=(0, 3) if idx % 2 == 0 else (3, 0),
                pady=2
            )

        ttk.Button(
            action_rail,
            text="Eliminar",
            command=self._delete_protocol_step,
            style="Danger.TButton"
        ).grid(
            row=3, column=0, columnspan=2,
            sticky="ew",
            pady=(2, 3)
        )

        ttk.Separator(
            action_rail,
            orient="horizontal"
        ).grid(
            row=4, column=0, columnspan=2,
            sticky="ew",
            pady=(5, 5)
        )

        ttk.Label(
            action_rail,
            text="Añadir pausa",
            style="Section.TLabel"
        ).grid(
            row=5, column=0, columnspan=2,
            sticky="w",
            pady=(0, 3)
        )

        pause_time_row = ttk.Frame(
            action_rail,
            style="Card.TFrame"
        )
        pause_time_row.grid(
            row=6, column=0, columnspan=2,
            sticky="ew",
            pady=(0, 3)
        )
        pause_time_row.columnconfigure(0, weight=1)
        ttk.Entry(
            pause_time_row,
            textvariable=self.protocol_pause_var,
            width=6
        ).grid(
            row=0, column=0,
            sticky="ew"
        )
        ttk.Label(
            pause_time_row,
            text="s"
        ).grid(
            row=0, column=1,
            sticky="w",
            padx=(4, 0)
        )

        ttk.Combobox(
            action_rail,
            textvariable=self.protocol_pause_color,
            values=["Fondo de adaptación", "Negro", "Gris medio"],
            state="readonly",
            width=17
        ).grid(
            row=7, column=0, columnspan=2,
            sticky="ew",
            pady=(0, 3)
        )

        ttk.Button(
            action_rail,
            text="＋ Añadir pausa",
            command=self._add_protocol_pause,
            style="Secondary.TButton"
        ).grid(
            row=8, column=0, columnspan=2,
            sticky="ew"
        )

        # Bottom command bar stays visible while the tree consumes the
        # remaining height.  Runtime pause is separated from save/execute.
        bottom = ttk.Frame(
            editor,
            style="Card.TFrame"
        )
        bottom.grid(
            row=2, column=0,
            sticky="ew",
            padx=9,
            pady=(1, 4)
        )
        bottom.columnconfigure(1, weight=1)

        self.protocol_pause_runtime_btn = ttk.Button(
            bottom,
            text="⏸ Pausar protocolo",
            command=self._toggle_protocol_pause,
            state="disabled",
            style="Secondary.TButton"
        )
        self.protocol_pause_runtime_btn.grid(
            row=0, column=0,
            sticky="w"
        )

        command_box = ttk.Frame(
            bottom,
            style="Card.TFrame"
        )
        command_box.grid(
            row=0, column=2,
            sticky="e"
        )

        ttk.Button(
            command_box,
            text="Guardar protocolo",
            command=self._save_protocol_draft,
            style="Accent.TButton"
        ).pack(
            side="left",
            padx=(0, 7)
        )

        self.protocol_execute_btn = ttk.Button(
            command_box,
            text="▶ Ejecutar protocolo",
            command=self._execute_protocol,
            state="disabled",
            style="Accent.TButton"
        )
        self.protocol_execute_btn.pack(
            side="left"
        )

        ttk.Label(
            editor,
            textvariable=self.protocol_builder_status,
            style="Muted.TLabel",
            wraplength=760
        ).grid(
            row=3, column=0,
            sticky="ew",
            padx=9,
            pady=(0, 7)
        )

        self._refresh_protocol_library()
        self._refresh_protocol_editor()

    def _protocol_clone(self, value):
        return json.loads(
            json.dumps(value)
        )

    def _new_protocol_id(self):
        return (
            "protocol_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S_%f"
            )
        )

    def _new_step_id(self):
        return (
            "step_"
            + datetime.now().strftime(
                "%Y%m%d_%H%M%S_%f"
            )
        )

    def _protocol_mark_dirty(self):
        self.protocol_dirty = True
        self._sync_protocol_header_from_vars()
        self._refresh_protocol_editor()

    def _sync_protocol_header_from_vars(self):
        if not hasattr(
            self,
            "protocol_name_var"
        ):
            return

        self.protocol_draft["name"] = (
            self.protocol_name_var.get().strip()
            or "Nuevo protocolo"
        )

        try:
            reps = int(
                self.protocol_reps_var.get()
            )
            if reps >= 1:
                self.protocol_draft[
                    "repetitions"
                ] = reps
        except Exception:
            pass

    def _refresh_protocol_library(
        self, select_id=None
    ):
        if not hasattr(
            self,
            "protocol_listbox"
        ):
            return

        self.protocol_listbox.delete(
            0, "end"
        )
        self.protocol_list_ids = []

        protocols = sorted(
            self.protocol_library.get(
                "protocols", []
            ),
            key=lambda p:
                str(p.get("name", "")).lower()
        )

        for protocol in protocols:
            pid = protocol.get("id")
            name = protocol.get(
                "name",
                "Sin nombre"
            )
            version = int(
                protocol.get("version", 1)
            )
            steps = len(
                protocol.get("steps", [])
            )

            self.protocol_listbox.insert(
                "end",
                f"{name}  ·  v{version}  ·  {steps} {self._tr('pasos')}"
            )
            self.protocol_list_ids.append(
                pid
            )

        if select_id is not None:
            try:
                idx = self.protocol_list_ids.index(
                    select_id
                )
                self.protocol_listbox.selection_clear(
                    0, "end"
                )
                self.protocol_listbox.selection_set(
                    idx
                )
                self.protocol_listbox.see(
                    idx
                )
            except Exception:
                pass

    def _selected_protocol_id(self):
        try:
            selection = (
                self.protocol_listbox.curselection()
            )
            if not selection:
                return None
            idx = int(selection[0])
            return self.protocol_list_ids[idx]
        except Exception:
            return None

    def _find_protocol(self, protocol_id):
        for protocol in self.protocol_library.get(
            "protocols", []
        ):
            if protocol.get("id") == protocol_id:
                return protocol
        return None

    def _confirm_discard_draft(self):
        if not self.protocol_dirty:
            return True

        return messagebox.askyesno(
            "Cambios sin guardar",
            (
                "El protocolo actual contiene cambios sin guardar.\n\n"
                "¿Quieres descartarlos?"
            ),
            parent=self
        )

    def _new_protocol(self):
        if not self._confirm_discard_draft():
            return

        self.protocol_draft = {
            "id": None,
            "name": self._tr("Nuevo protocolo"),
            "version": 0,
            "repetitions": 1,
            "steps": [],
        }
        self.protocol_dirty = False
        self.protocol_edit_step_index = None

        self.protocol_name_var.set(
            "Nuevo protocolo"
        )
        self.protocol_reps_var.set(
            "1"
        )
        self._refresh_protocol_edit_buttons()
        self._refresh_protocol_editor()

        self.protocol_builder_status.set(
            "Nuevo protocolo preparado. "
            "Configura cualquier estímulo y pulsa "
            "«Añadir al protocolo»."
        )

    def _load_selected_protocol(self):
        pid = self._selected_protocol_id()
        if pid is None:
            messagebox.showinfo(
                "Protocolos",
                "Selecciona un protocolo guardado."
            )
            return

        if not self._confirm_discard_draft():
            return

        protocol = self._find_protocol(
            pid
        )
        if protocol is None:
            return

        self.protocol_draft = (
            self._protocol_clone(
                protocol
            )
        )
        self.protocol_dirty = False
        self.protocol_edit_step_index = None

        self.protocol_name_var.set(
            self.protocol_draft.get(
                "name", "Sin nombre"
            )
        )
        self.protocol_reps_var.set(
            str(
                self.protocol_draft.get(
                    "repetitions", 1
                )
            )
        )

        self._refresh_protocol_edit_buttons()
        self._refresh_protocol_editor()

        self.protocol_builder_status.set(
            (
                f"Protocolo cargado · v"
                f"{self.protocol_draft.get('version', 1)}. "
                "Puedes modificarlo y guardarlo como una nueva revisión."
            )
        )

    def _duplicate_selected_protocol(self):
        pid = self._selected_protocol_id()
        if pid is None:
            messagebox.showinfo(
                "Protocolos",
                "Selecciona un protocolo guardado."
            )
            return

        protocol = self._find_protocol(
            pid
        )
        if protocol is None:
            return

        if not self._confirm_discard_draft():
            return

        duplicate = self._protocol_clone(
            protocol
        )
        duplicate["id"] = None
        duplicate["version"] = 0
        duplicate["name"] = (
            str(
                duplicate.get(
                    "name", "Protocolo"
                )
            )
            + " (copia)"
        )
        duplicate.pop(
            "created_at", None
        )
        duplicate.pop(
            "modified_at", None
        )

        for step in duplicate.get(
            "steps", []
        ):
            step["id"] = (
                self._new_step_id()
            )

        self.protocol_draft = duplicate
        self.protocol_dirty = True
        self.protocol_edit_step_index = None

        self.protocol_name_var.set(
            duplicate["name"]
        )
        self.protocol_reps_var.set(
            str(
                duplicate.get(
                    "repetitions", 1
                )
            )
        )

        self._refresh_protocol_edit_buttons()
        self._refresh_protocol_editor()

        self.protocol_builder_status.set(
            "Copia creada como borrador. "
            "Guárdala para añadirla a la biblioteca."
        )

    def _delete_selected_protocol(self):
        pid = self._selected_protocol_id()
        if pid is None:
            messagebox.showinfo(
                "Protocolos",
                "Selecciona un protocolo guardado."
            )
            return

        protocol = self._find_protocol(
            pid
        )
        if protocol is None:
            return

        if not messagebox.askyesno(
            "Eliminar protocolo",
            (
                f"¿Eliminar «{protocol.get('name', 'Sin nombre')}»?\n\n"
                "Esta acción elimina la definición guardada, "
                "no los CSV de experimentos anteriores."
            ),
            parent=self
        ):
            return

        self.protocol_library["protocols"] = [
            p
            for p in self.protocol_library.get(
                "protocols", []
            )
                    extra = (
                        f"Static · 1ª orientación {angle:g}° · "
                        f"fase {self.gr_phase.get()} ciclos · "
                        f"{self.gr_sf.get()} c/°"
                    )
                else:
                    canvas.create_text(
                        cx,
                        150,
                        text="fase ↔ fase + 0,5 ciclos",
                        fill="#F0A44B",
                        font=(
                            "Segoe UI Semibold",
                            9
                        )
                    )
                    extra = (
                        f"Phase Reversal · 1ª orientación {angle:g}° · "
                        f"{self.gr_reversal_hz.get()} Hz"
                    )

            else:
                radius = max(
                    28.0,
                    min(
                        72.0,
                        self._safe_float(
                            self.gr_gabor_size,
                            30
                        ) * 2.0
                    )
                )

                canvas.create_oval(
                    cx - radius,
                    cy - radius,
                    cx + radius,
                    cy + radius,
                    outline="#5E6B72",
                    width=1
                )

                for offset in range(
                    -70, 71, 18
                ):
                    if abs(offset) > radius:
                        continue

                    half_chord = math.sqrt(
                        max(
                            0.0,
                            radius * radius
                            - offset * offset
                        )
                    )
                    ox = mx * offset
                    oy = my * offset

                    canvas.create_line(
                        cx + ox - sx * half_chord,
                        cy + oy - sy * half_chord,
                        cx + ox + sx * half_chord,
                        cy + oy + sy * half_chord,
                        fill=color_a,
                        width=8
                    )

                if mode == "Drifting Gabor":
                    canvas.create_line(
                        cx - mx * 46,
                        cy - my * 46,
                        cx + mx * 46,
                        cy + my * 46,
                        fill="#F0A44B",
                        width=3,
                        arrow="last",
                        arrowshape=(10, 12, 5)
                    )
                    extra = (
                        f"Drifting Gabor · 1ª dirección {angle:g}° · "
                        f"{self.gr_tf.get()} Hz · "
                        f"tamaño {self.gr_gabor_size.get()}°"
                    )
                else:
                    extra = (
                        f"Static Gabor · 1ª orientación {angle:g}° · "
                        f"tamaño {self.gr_gabor_size.get()}° · "
                        f"σ {self.gr_gabor_sigma.get()}°"
                    )

            self._set_preview_summary(
                key,
                extra
            )

        elif key == "dense_noise":
            self._preview_monitor(
                canvas,
                "#808080"
            )

            family = self.dn_family.get()
            distribution = self.dn_mode.get()

            if family == "Gaussian White Noise":
                distribution = "Gaussian"

            contrast = max(
                0.0,
                min(
                    1.0,
                    self._safe_float(
                        self.dn_contrast,
                        1.0
                    )
                )
            )

            low_rgb = hex_to_rgb255(
                self.dn_low_hex.get()
            )
            mid_rgb = hex_to_rgb255(
                self.dn_mid_hex.get()
            )
            high_rgb = hex_to_rgb255(
                self.dn_high_hex.get()
            )

            rng = random.Random(
                self._safe_int(
                    self.dn_seed,
                    12345
                )
            )

            def preview_noise_color():
                if distribution == "Binary":
                    return (
                        self.dn_low_hex.get()
                        if rng.randrange(2) == 0
                        else self.dn_high_hex.get()
                    )

                if distribution == "Ternary":
                    return [
                        self.dn_low_hex.get(),
                        self.dn_mid_hex.get(),
                        self.dn_high_hex.get(),
                    ][
                        rng.randrange(3)
                    ]

                # Gaussian: mean = Color medio. With contrast=1,
                # approximately ±3 sigma reaches low/high.
                v = max(
                    -1.0,
                    min(
                        1.0,
                        rng.gauss(
                            0.0,
                            1.0 / 3.0
                        )
                    )
                )
                v *= contrast

                if v < 0:
                    w = -v
                    rgb = [
                        mid_rgb[i]
                        + w
                        * (
                            low_rgb[i]
                            - mid_rgb[i]
                        )
                        for i in range(3)
                    ]
                else:
                    w = v
                    rgb = [
                        mid_rgb[i]
                        + w
                        * (
                            high_rgb[i]
                            - mid_rgb[i]
                        )
                        for i in range(3)
                    ]

                return rgb255_to_hex([
                    int(
                        round(
                            max(
                                0,
                                min(255, x)
                            )
                        )
                    )
                    for x in rgb
                ])

            x0, y0 = 18, 18
            x1, y1 = 312, 175
            cols, rows = 10, 6
            cw = (x1 - x0) / cols
            ch = (y1 - y0) / rows

            for r in range(rows):
                for c in range(cols):
                    color = (
                        preview_noise_color()
                    )
                    canvas.create_rectangle(
                        x0 + c * cw,
                        y0 + r * ch,
                        x0 + (c + 1) * cw,
                        y0 + (r + 1) * ch,
                        fill=color,
                        outline=color
                    )

            canvas.create_rectangle(
                x0, y0, x1, y1,
                outline="#5E6B72",
                width=2
            )

            if family == "Dense White Noise":
                family_label = (
                    f"Dense White Noise · {distribution}"
                )
            elif family == "Gaussian White Noise":
                family_label = (
                    "Gaussian White Noise"
                )
            else:
                family_label = (
                    f"Frozen Noise · {distribution}"
                )

            dense_extra = (
                f"{family_label} · "
                f"{self.dn_update_hz.get()} Hz · "
                f"check {self.dn_check_size.get()}°"
            )

            if family == "Frozen Noise":
                dense_extra += (
                    "\nLa misma secuencia se repite en cada repetición."
                )

            try:
                requested = self._safe_float(
                    self.dn_update_hz
                )
                if (
                    self.current_measured_hz is not None
                    and requested > self.current_measured_hz
                ):
                    dense_extra += (
                        "\n⚠ Limitado por refresco: "
                        f"máx. {self.current_measured_hz:.2f} Hz"
                    )
            except Exception:
                pass

            self._set_preview_summary(
                key,
                dense_extra
            )

        elif key == "rf_mapping":
            mode = self.rf_mode.get()
            bg = self.rf_bg_hex.get()
            stim = self.rf_stim_hex.get()

            if mode == "Sparse Noise":
                bg = "#808080"

            self._preview_monitor(
                canvas, bg
            )

            px = 165 + max(
                -120,
                min(
                    120,
                    self._safe_float(
                        self.rf_x
                    ) * 4.0
                )
            )
            py = 96 - max(
                -60,
                min(
                    60,
                    self._safe_float(
                        self.rf_y
                    ) * 4.0
                )
            )

            if mode == "Spot":
                d = max(
                    12,
                    min(
                        125,
                        self._safe_float(
                            self.rf_spot_diameter,
                            8
                        ) * 5.0
                    )
                )
                canvas.create_oval(
                    px - d / 2, py - d / 2,
                    px + d / 2, py + d / 2,
                    fill=stim,
                    outline="#5E6B72",
                    width=1
                )
                extra = (
                    f"Spot · {self.rf_spot_diameter.get()}° · "
                    f"X {self.rf_x.get()}° / Y {self.rf_y.get()}°"
                )

            elif mode == "Annulus":
                outer = max(
                    30,
                    min(
                        145,
                        self._safe_float(
                            self.rf_annulus_outer,
                            20
                        ) * 4.5
                    )
                )
                inner = max(
                    8,
                    min(
                        outer - 5,
                        self._safe_float(
                            self.rf_annulus_inner,
                            8
                        ) * 4.5
                    )
                )
                canvas.create_oval(
                    px - outer / 2, py - outer / 2,
                    px + outer / 2, py + outer / 2,
                    fill=stim,
                    outline="#5E6B72"
                )
                canvas.create_oval(
                    px - inner / 2, py - inner / 2,
                    px + inner / 2, py + inner / 2,
                    fill=bg,
                    outline=bg
                )
                extra = (
                    f"Annulus · {self.rf_annulus_inner.get()}° → "
                    f"{self.rf_annulus_outer.get()}°"
                )

            elif mode == "Size series":
                try:
                    vals = [
                        float(x.strip())
                        for x in self.rf_size_series.get().split(",")
                        if x.strip()
                    ]
                except Exception:
                    vals = [2, 4, 8, 16]

                vals = vals or [2, 4, 8, 16]
                max_v = max(vals)
                for value in sorted(vals, reverse=True):
                    d = 20 + 105 * value / max_v
                    canvas.create_oval(
                        px - d / 2, py - d / 2,
                        px + d / 2, py + d / 2,
                        outline=stim,
                        width=2
                    )
                extra = (
                    f"Size series · {len(vals)} diámetros"
                )

            elif mode == "Annulus Size series":
                try:
                    vals = [
                        float(x.strip())
                        for x in self.rf_annulus_size_series.get().split(",")
                        if x.strip()
                    ]
                except Exception:
                    vals = [8, 12, 16, 24, 32, 40]

                vals = vals or [8, 12, 16, 24, 32, 40]
                thickness = max(
                    0.1,
                    self._safe_float(
                        self.rf_annulus_series_thickness,
                        2.0
                    )
                )
                max_v = max(vals)

                for value in sorted(vals, reverse=True):
                    outer = 20 + 105 * value / max_v
                    scale = outer / value
                    thick_px = max(
                        2.0,
                        thickness * scale
                    )
                    inner = max(
                        1.0,
                        outer - 2.0 * thick_px
                    )
                    canvas.create_oval(
                        px - outer / 2, py - outer / 2,
                        px + outer / 2, py + outer / 2,
                        outline=stim,
                        width=2
                    )
                    canvas.create_oval(
                        px - inner / 2, py - inner / 2,
                        px + inner / 2, py + inner / 2,
                        outline=bg,
                        width=2
                    )

                extra = (
                    f"Annulus Size series · {len(vals)} diámetros · "
                    f"grosor {self.rf_annulus_series_thickness.get()}°"
                )

            else:
                x0, y0 = 28, 28
                x1, y1 = 302, 163
                step_grid = 34
                for gx in range(x0, x1 + 1, step_grid):
                    canvas.create_line(
                        gx, y0, gx, y1,
                        fill="#9AA6AC",
                        dash=(2, 4)
                    )
                for gy in range(y0, y1 + 1, step_grid):
                    canvas.create_line(
                        x0, gy, x1, gy,
                        fill="#9AA6AC",
                        dash=(2, 4)
                    )

                square = 28
                example_color = (
                    "#FFFFFF"
                    if self.rf_sparse_polarity.get()
                    != "Dark"
                    else "#000000"
                )
                canvas.create_rectangle(
                    151, 69,
                    151 + square,
                    69 + square,
                    fill=example_color,
                    outline="#5E6B72"
                )
                extra = (
                    f"Sparse Noise · {self.rf_sparse_square.get()}° · "
                    f"{self.rf_sparse_polarity.get()} · "
                    f"{self.rf_sparse_trials.get()} presentaciones/rep"
                )

            self._set_preview_summary(
                key,
                extra
            )

        elif key == "chirp_intensity":
            canvas.delete("all")

            mode = self.temporal_mode.get()
            high = self.ch_on_hex.get()
            low = self.ch_off_hex.get()

            if mode == "Chirp + Intensity":
                canvas.create_text(
                    18, 15,
                    anchor="nw",
                    text="A        B             C                    D",
                    fill=self.palette["muted"],
                    font=("Segoe UI Semibold", 9)
                )

                baseline_y = 145
                canvas.create_line(
                    18, baseline_y,
                    312, baseline_y,
                    fill="#8A989F",
                    width=2
                )
                canvas.create_line(
                    20, baseline_y,
                    52, baseline_y,
                    fill="#808080",
                    width=7
                )
                canvas.create_rectangle(
                    58, 58, 82, 145,
                    fill=high,
                    outline=""
                )
                canvas.create_rectangle(
                    82, 122, 105, 145,
                    fill=low,
                    outline=""
                )
                canvas.create_line(
                    106, baseline_y,
                    125, baseline_y,
                    fill="#808080",
                    width=5
                )

                x = 130
                gap = 10.0
                while x < 225:
                    canvas.create_rectangle(
                        x, 72,
                        x + max(
                            2,
                            gap * 0.45
                        ),
                        145,
                        fill=high,
                        outline=""
                    )
                    x += gap
                    gap = max(
                        3.0,
                        gap * 0.90
                    )

                canvas.create_line(
                    226, baseline_y,
                    239, baseline_y,
                    fill="#808080",
                    width=5
                )

                for i in range(6):
                    alpha = (
                        (i + 1) / 6
                    )
                    color = self._blend_hex(
                        low,
                        high,
                        alpha
                    )
                    x0 = (
                        244
                        + i * 11
                    )
                    canvas.create_rectangle(
                        x0,
                        78,
                        x0 + 6,
                        145,
                        fill=color,
                        outline=""
                    )

                canvas.create_text(
                    165,
                    178,
                    text=(
                        "ON/OFF inicial → chirp → "
                        "rampa de intensidad"
                    ),
                    fill=self.palette["text"],
                    font=("Segoe UI Semibold", 9)
                )

                extra = (
                    f"Chirp {self.ch_fmin.get()}→"
                    f"{self.ch_fmax.get()} Hz · "
                    f"Intensity {self.ch_ifreq.get()} Hz"
                )

            elif mode == "Sinusoidal Flicker":
                self._preview_monitor(
                    canvas,
                    "#808080"
                )
                pts = []
                for i in range(0, 281, 4):
                    x = 25 + i
                    y = (
                        96
                        - 48
                        * math.sin(
                            2
                            * math.pi
                            * i
                            / 80.0
                        )
                    )
                    pts.extend(
                        [x, y]
                    )
                canvas.create_line(
                    *pts,
                    fill="#F0A44B",
                    width=3,
                    smooth=True
                )
                canvas.create_text(
                    165,
                    174,
                    text="modulación sinusoidal continua",
                    fill=self.palette["text"],
                    font=("Segoe UI Semibold", 9)
                )
                extra = (
                    f"Sinusoidal · {self.ch_temp_frequency.get()} Hz · "
                    f"{self.ch_temp_contrast.get()} %"
                )

            elif mode == "Gaussian Flicker":
                self._preview_monitor(
                    canvas,
                    "#808080"
                )
                rng = random.Random(
                    self._safe_int(
                        self.ch_temp_seed,
                        54321
                    )
                )
                pts = []
                x0 = 28
                for i in range(42):
                    x = (
                        x0
                        + i * 6.5
                    )
                    y = (
                        96
                        - max(
                            -52,
                            min(
                                52,
                                rng.gauss(
                                    0,
                                    20
                                )
                            )
                        )
                    )
                    pts.extend(
                        [x, y]
                    )
                canvas.create_line(
                    *pts,
                    fill="#F0A44B",
                    width=2
                )
                canvas.create_text(
                    165,
                    174,
                    text="niveles temporales gaussianos independientes",
                    fill=self.palette["text"],
                    font=("Segoe UI Semibold", 9)
                )
                extra = (
                    f"Gaussian · {self.ch_temp_update_hz.get()} Hz nominal · "
                    f"{self.ch_temp_contrast.get()} %"
                )

            else:
                self._preview_monitor(
                    canvas,
                    "#808080"
                )
                try:
                    levels = [
                        float(
                            v.strip()
                        )
                        for v
                        in self.ch_contrast_levels.get().replace(
                            ";", ","
                        ).split(",")
                        if v.strip()
                    ]
                except Exception:
                    levels = [
                        10,
                        25,
                        50,
                        100
                    ]

                levels = (
                    levels[:6]
                    if levels
                    else [
                        10,
                        25,
                        50,
                        100
                    ]
                )

                width = (
                    250
                    / max(
                        1,
                        len(levels)
                    )
                )
                for idx, pct in enumerate(
                    levels
                ):
                    amp = (
                        48
                        * max(
                            0.0,
                            min(
                                100.0,
                                pct
                            )
                        )
                        / 100.0
                    )
                    cx = (
                        40
                        + idx * width
                    )
                    canvas.create_line(
                        cx,
                        96 - amp,
                        cx,
                        96 + amp,
                        fill="#F0A44B",
                        width=max(
                            3,
                            int(
                                width * 0.25
                            )
                        )
                    )
                    canvas.create_text(
                        cx,
                        160,
                        text=f"{pct:g}%",
                        fill=self.palette["text"],
                        font=("Segoe UI", 8)
                    )

                canvas.create_text(
                    165,
                    184,
                    text="amplitud de flicker por contraste",
                    fill=self.palette["text"],
                    font=("Segoe UI Semibold", 9)
                )
                extra = (
                    f"Contrast Series · {self.ch_temp_frequency.get()} Hz · "
                    f"{self.ch_contrast_levels.get()} %"
                )

            self._set_preview_summary(
                key,
                extra
            )


    def _stop_preview(
        self, redraw=True
    ):
        if self.preview_after_id is not None:
            try:
                self.after_cancel(
                    self.preview_after_id
                )
            except Exception:
                pass
        key = self.preview_running_key
        self.preview_after_id = None
        self.preview_running_key = None
        self.preview_step = 0

        if redraw and key:
            self._draw_preview_static(key)

    def _start_preview(self, key):
        self._stop_preview(
            redraw=False
        )
        self._stop_easter_egg(
            redraw=False
        )
        self.preview_running_key = key
        self.preview_step = 0
        self._animate_preview()

    def _animate_preview(self):
        key = self.preview_running_key
        if not key:
            return

        canvas = self.preview_canvases.get(
            key
        )
        if canvas is None:
            self._stop_preview(
                redraw=False
            )
            return

        step = self.preview_step
        delay = 90
        done = False

        if key == "on_off":
            seq = [
                ("#808080", "gris"),
                (
                    self.on_on_hex.get(),
                    "ON"
                ),
                ("#808080", "gris"),
                (
                    self.on_off_hex.get(),
                    "OFF"
                ),
                ("#808080", "gris"),
            ]

            idx = min(
                len(seq) - 1,
                step // 5
            )
            color, label = seq[idx]
            self._preview_monitor(
                canvas, color
            )
            canvas.create_text(
                165, 96,
                text=label,
                fill=best_text_color(color),
                font=("Segoe UI Semibold", 21)
            )
            delay = 110
            done = step >= 24

        elif key == "motion":
            mode = self.motion_mode.get()
            bg = self.motion_background_hex.get()
            fg = self.motion_stimulus_hex.get()

            self._preview_monitor(
                canvas, bg
            )

            if mode in (
                "Moving Bar",
                "Moving Spot",
            ):
                angle = self._preview_first_angle(
                    self.motion_dirs.get()
                )
                theta = math.radians(angle)
                dx = math.cos(theta)
                dy = -math.sin(theta)
                u = min(
                    1.0,
                    step / 34.0
                )
                cx = 165 + (
                    (u - 0.5)
                    * 220
                    * dx
                )
                cy = 96 + (
                    (u - 0.5)
                    * 120
                    * dy
                )

                if mode == "Moving Bar":
                    ax, ay = -dy, dx
                    half_l = 68
                    half_w = 9
                    points = [
                        (cx + ax * half_l + dx * half_w,
                         cy + ay * half_l + dy * half_w),
                        (cx - ax * half_l + dx * half_w,
                         cy - ay * half_l + dy * half_w),
                        (cx - ax * half_l - dx * half_w,
                         cy - ay * half_l - dy * half_w),
                        (cx + ax * half_l - dx * half_w,
                         cy + ay * half_l - dy * half_w),
                    ]
                    flat = [
                        value
                        for point in points
                        for value in point
                    ]
                    canvas.create_polygon(
                        *flat,
                        fill=fg,
                        outline="#5E6B72"
                    )
                else:
                    d = max(
                        14,
                        min(
                            70,
                            self._safe_float(
                                self.motion_spot_diameter,
                                8
                            ) * 3.5
                        )
                    )
                    canvas.create_oval(
                        cx - d / 2,
                        cy - d / 2,
                        cx + d / 2,
                        cy + d / 2,
                        fill=fg,
                        outline="#5E6B72"
                    )

                delay = 45
                done = step >= 34

            elif mode in (
                "Looming",
                "Receding",
            ):
                small = max(
                    10.0,
                    min(
                        45.0,
                        self._safe_float(
                            self.motion_small_diameter,
                            2
                        ) * 3.0
                    )
                )
                large = max(
                    small + 10.0,
                    min(
                        145.0,
                        self._safe_float(
                            self.motion_large_diameter,
                            40
                        ) * 3.0
                    )
                )

                u = min(
                    1.0,
                    step / 34.0
                )
                if mode == "Looming":
                    d = (
                        small
                        + (large - small) * u
                    )
                else:
                    d = (
                        large
                        - (large - small) * u
                    )

                cx, cy = 165, 96
                canvas.create_oval(
                    cx - d / 2,
                    cy - d / 2,
                    cx + d / 2,
                    cy + d / 2,
                    fill=fg,
                    outline="#5E6B72"
                )
                delay = 45
                done = step >= 34

            else:
                small_deg = max(
                    0.1,
                    self._safe_float(
                        self.motion_annulus_small_diameter,
                        8
                    )
                )
                large_deg = max(
                    small_deg + 0.1,
        finally:
            self.after(
                120,
                self.poll_worker
            )

    def on_close(self):
        self._restore_global_gamma_safely()
        if self._trigger_test_active:
            self._finish_trigger_test(cancelled=True, silent=True)
        self.active_command_id = None
        self.pending_config = None

        self._stop_easter_egg(
            redraw=False
        )

        try:
            if self.splash is not None:
                try:
                    self.splash_progress.stop()
                except Exception:
                    pass
                try:
                    self.splash.destroy()
                except Exception:
                    pass
                self.splash = None
        except Exception:
            pass

        for aid in self.ttl_test_after_ids:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self.ttl_test_after_ids = []

        # Keep the safety layer until PsychoPy is fully stopped.
        self.stop_display_worker(
            intentional=True
        )
        self.stop_safety_window()

        for path in (
            self.worker_file,
            self.safety_file
        ):
            try:
                if (
                    path is not None
                    and path.exists()
                ):
                    path.unlink(missing_ok=True)
            except Exception:
                pass

        try:
            self.abort_path.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        try:
            self.protocol_pause_path.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        self.destroy()

SAFETY_SOURCE_EMBEDDED = 'import sys\nimport tkinter as tk\n\nx = int(sys.argv[1])\ny = int(sys.argv[2])\nw = int(sys.argv[3])\nh = int(sys.argv[4])\ncolor = str(sys.argv[5]) if len(sys.argv) > 5 else "#000000"\n\nroot = tk.Tk()\nroot.configure(bg=color)\nroot.overrideredirect(True)\nroot.geometry(f"{w}x{h}+0+0")\nroot.update_idletasks()\nroot.update()\n\ntry:\n    import ctypes\n\n    user32 = ctypes.windll.user32\n    user32.GetParent.argtypes = [ctypes.c_void_p]\n    user32.GetParent.restype = ctypes.c_void_p\n    user32.SetWindowPos.argtypes = [\n        ctypes.c_void_p, ctypes.c_void_p,\n        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,\n        ctypes.c_uint\n    ]\n    user32.SetWindowPos.restype = ctypes.c_int\n\n    # Tkinter\'s winfo_id() can refer to the client child window on Windows.\n    # GetParent gives us the real top-level HWND which can be positioned at\n    # negative virtual-desktop coordinates such as x=-1440.\n    client_hwnd = ctypes.c_void_p(root.winfo_id())\n    top_hwnd = user32.GetParent(client_hwnd)\n    if not top_hwnd:\n        top_hwnd = client_hwnd\n\n    HWND_NOTOPMOST = ctypes.c_void_p(-2 & 0xFFFFFFFFFFFFFFFF)\n    SWP_NOACTIVATE = 0x0010\n    SWP_SHOWWINDOW = 0x0040\n\n    ok = user32.SetWindowPos(\n        top_hwnd,\n        HWND_NOTOPMOST,\n        x, y, w, h,\n        SWP_NOACTIVATE | SWP_SHOWWINDOW\n    )\n\n    if not ok:\n        raise OSError("SetWindowPos no pudo situar la capa de seguridad.")\n\nexcept Exception:\n    # The safety process remains useful as a safe backing layer even if the\n    # native placement fails, but on Windows the block above is the intended path.\n    pass\n\nroot.mainloop()'
WORKER_SOURCE_EMBEDDED = 'import time as _ftdi_time\n\n\nclass MaxOneFTDI:\n    """Shared FTDI backend for the operator diagnostic and display worker.\n\n    Logical signals use the validated inverted VisExpMan polarity.\n    USB/driver latency is measured by the experiment, not assumed zero.\n    """\n\n    FRAME_PULSE_MIN_S = 0.001\n\n    def __init__(self, port="auto"):\n        self.requested_port = str(port or "auto").strip() or "auto"\n        self.resolved_port = ""\n        self.serial = None\n        self._frame_high_at = None\n\n    def open(self):\n        if self.serial is not None:\n            raise RuntimeError("El puerto FTDI ya está abierto.")\n        try:\n            import serial\n        except ImportError as exc:\n            raise RuntimeError(\n                "MaxOne Legacy FTDI requiere PySerial. Instálalo en el "\n                "entorno de este notebook: %pip install pyserial"\n            ) from exc\n\n        port = self.requested_port\n        if port.lower() in ("auto", "automatico", "automático"):\n            from serial.tools import list_ports\n            matches = [\n                p for p in list_ports.comports()\n                if getattr(p, "vid", None) == 0x0403\n                and getattr(p, "pid", None) == 0x6001\n            ]\n            if not matches:\n                raise RuntimeError("No se encuentra el FTDI MaxOne (0403:6001).")\n            if len(matches) != 1:\n                devices = ", ".join(str(p.device) for p in matches)\n                raise RuntimeError(\n                    f"Hay varios FTDI 0403:6001 ({devices}). "\n                    "Indica el puerto del montaje en el campo Puerto."\n                )\n            port = str(matches[0].device)\n\n        try:\n            # Configure the requested rest state before opening where the\n            # driver permits it; reapply both outputs immediately afterwards.\n            self.serial = serial.Serial(\n                port=None, baudrate=9600, timeout=0, write_timeout=0,\n                rtscts=False, dsrdtr=False, xonxoff=False,\n            )\n            self.serial.rts = True\n            self.serial.break_condition = True\n            self.serial.port = port\n            self.serial.open()\n            self.resolved_port = port\n            self.idle(strict=True)\n        except Exception as exc:\n            self.close()\n            raise RuntimeError(\n                f"No se pudo abrir/configurar {port}: {type(exc).__name__}: {exc}"\n            ) from exc\n        return self.resolved_port\n\n    def _set(self, signal, logical):\n        if self.serial is None:\n            raise RuntimeError("El backend MaxOne FTDI no está abierto.")\n        physical = not bool(logical)\n        if signal == "frame":\n            self.serial.rts = physical\n        else:\n            self.serial.break_condition = physical\n\n    def set_frame(self, logical):\n        self._set("frame", logical)\n        self._frame_high_at = _ftdi_time.perf_counter() if logical else None\n\n    def set_event(self, logical):\n        self._set("event", logical)\n\n    def frame_high(self):\n        self.set_frame(1)\n\n    def frame_low(self):\n        # Keep the requested RTS HIGH for at least 1 ms after the driver call.\n        # A bounded short wait avoids scheduling a Timer across later frames.\n        # Physical width can be longer; validate it in /bits on the real setup.\n        if self._frame_high_at is not None:\n            deadline = self._frame_high_at + self.FRAME_PULSE_MIN_S\n            while _ftdi_time.perf_counter() < deadline:\n                pass\n        self.set_frame(0)\n\n    def idle(self, strict=False):\n        errors = []\n        if self.serial is not None:\n            # Attempt BOTH lines even if one write fails.\n            for name, action in (("FRAME", self.set_frame), ("EVENT", self.set_event)):\n                try:\n                    action(0)\n                except Exception as exc:\n                    errors.append(f"{name}: {type(exc).__name__}: {exc}")\n        self._frame_high_at = None\n        if errors and strict:\n            raise RuntimeError("; ".join(errors))\n        return errors\n\n    def close(self):\n        errors = self.idle()\n        ser, self.serial = self.serial, None\n        if ser is not None:\n            try:\n                ser.close()\n            except Exception as exc:\n                errors.append(f"Cerrar FTDI: {type(exc).__name__}: {exc}")\n        return errors\n\n\nclass GenericTTLFTDI:\n    """Shared FTDI backend for the generic trigger box and display worker.\n\n    Validated hardware mapping (active-low FTDI control-line polarity):\n      UNIVERSAL / FRAME_SYNC -> RTS#  (ser.rts = False)\n      AUX EVENT              -> TX/BREAK (break_condition = False)\n\n    Encoding on UNIVERSAL is generated in software:\n      visual flip -> FRAME pulse on RTS\n      condition onset -> RTS EVENT pulse ~3 ms later + simultaneous BREAK EVENT\n    The EVENT replication onto RTS uses _set() directly so it never overwrites\n    the visual FRAME anchor used for timing and logging.\n    """\n\n    FRAME_PULSE_MIN_S = 0.001\n    EVENT_PULSE_MIN_S = 0.001\n    EVENT_OFFSET_S = 0.003\n\n    def __init__(self, port="auto"):\n        self.requested_port = str(port or "auto").strip() or "auto"\n        self.resolved_port = ""\n        self.serial = None\n        self._frame_high_at = None\n        self._last_frame_anchor = None\n        self._event_high_at = None\n\n    @staticmethod\n    def _wait_until(deadline):\n        """Hybrid sleep/spin wait, matching the validated UNIVERSAL tester."""\n        while True:\n            remaining = float(deadline) - _ftdi_time.perf_counter()\n            if remaining <= 0:\n                return\n            if remaining > 0.002:\n                _ftdi_time.sleep(max(0.0, remaining - 0.001))\n            # Final sub-millisecond section intentionally busy-waits.\n\n    def open(self):\n        if self.serial is not None:\n            raise RuntimeError("El puerto FTDI ya está abierto.")\n        try:\n            import serial\n        except ImportError as exc:\n            raise RuntimeError(\n                "Trigger Box TTL genérica requiere PySerial. Instálalo en el "\n                "entorno de este notebook: %pip install pyserial"\n            ) from exc\n\n        port = self.requested_port\n        if port.lower() in ("auto", "automatico", "automático"):\n            from serial.tools import list_ports\n            matches = [\n                p for p in list_ports.comports()\n                if getattr(p, "vid", None) == 0x0403\n                and getattr(p, "pid", None) == 0x6001\n            ]\n            if not matches:\n                raise RuntimeError("No se encuentra el FTDI de la Trigger Box (0403:6001).")\n            if len(matches) != 1:\n                devices = ", ".join(str(p.device) for p in matches)\n                raise RuntimeError(\n                    f"Hay varios FTDI 0403:6001 ({devices}). "\n                    "Indica manualmente el puerto de la Trigger Box."\n                )\n            port = str(matches[0].device)\n\n        try:\n            self.serial = serial.Serial(\n                port=None, baudrate=9600, timeout=0, write_timeout=0,\n                rtscts=False, dsrdtr=False, xonxoff=False,\n            )\n            # Validated idle polarity: both FTDI lines physically inactive.\n            self.serial.rts = True\n            self.serial.break_condition = True\n            self.serial.port = port\n            self.serial.open()\n            self.resolved_port = port\n            self.idle(strict=True)\n        except Exception as exc:\n            self.close()\n            raise RuntimeError(\n                f"No se pudo abrir/configurar {port}: {type(exc).__name__}: {exc}"\n            ) from exc\n        return self.resolved_port\n\n    def _set(self, signal, logical):\n        if self.serial is None:\n            raise RuntimeError("El backend FTDI genérico no está abierto.")\n        physical = not bool(logical)\n        if signal == "frame":\n            self.serial.rts = physical\n        elif signal == "event":\n            self.serial.break_condition = physical\n        else:\n            raise ValueError(f"Señal FTDI desconocida: {signal}")\n\n    def set_frame(self, logical):\n        self._set("frame", logical)\n        now = _ftdi_time.perf_counter()\n        if logical:\n            self._frame_high_at = now\n            self._last_frame_anchor = now\n        else:\n            self._frame_high_at = None\n        return now\n\n    def set_event(self, logical):\n        self._set("event", logical)\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = now if logical else None\n        return now\n\n    def frame_high(self):\n        return self.set_frame(1)\n\n    def frame_low(self):\n        # Keep FRAME HIGH for at least 1 ms. The validated hardware produced\n        # ~1.1-1.35 ms physical pulses with this requested width.\n        if self._frame_high_at is not None:\n            self._wait_until(self._frame_high_at + self.FRAME_PULSE_MIN_S)\n        return self.set_frame(0)\n\n    def _event_pair_high(self):\n        """Assert EVENT on AUX EVENT and replicate it onto UNIVERSAL."""\n        if self.serial is None:\n            raise RuntimeError("El backend FTDI genérico no está abierto.")\n        errors = []\n        # RTS carries UNIVERSAL. BREAK carries the independent AUX EVENT.\n        # Use _set() directly for RTS so the visual FRAME anchor is NOT changed.\n        for signal in ("frame", "event"):\n            try:\n                self._set(signal, 1)\n            except Exception as exc:\n                errors.append(f"{signal}: {type(exc).__name__}: {exc}")\n        if errors:\n            # Best-effort fail-safe: release both outputs before propagating.\n            for signal in ("event", "frame"):\n                try:\n                    self._set(signal, 0)\n                except Exception:\n                    pass\n            raise RuntimeError("; ".join(errors))\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = now\n        return now\n\n    def _event_pair_low(self):\n        """Release AUX EVENT and UNIVERSAL after an EVENT pulse."""\n        errors = []\n        # Release UNIVERSAL first, then AUX EVENT; both operations are attempted.\n        for signal in ("frame", "event"):\n            try:\n                self._set(signal, 0)\n            except Exception as exc:\n                errors.append(f"{signal}: {type(exc).__name__}: {exc}")\n        now = _ftdi_time.perf_counter()\n        self._event_high_at = None\n        if errors:\n            raise RuntimeError("; ".join(errors))\n        return now\n\n    def pulse_event_now(self, width_s=None):\n        """Generate EVENT simultaneously on UNIVERSAL (RTS) and AUX EVENT (BREAK)."""\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        high_at = self._event_pair_high()\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            low_at = self._event_pair_low()\n        return {\n            "event_high_perf_s": high_at,\n            "event_low_perf_s": low_at,\n            "event_width_s": max(0.0, low_at - high_at),\n        }\n\n    def pulse_event_after_frame(self, offset_s=None, width_s=None):\n        """Emit EVENT as the second UNIVERSAL pulse after the last visual FRAME."""\n        if self._last_frame_anchor is None:\n            raise RuntimeError(\n                "No hay un FRAME_SYNC previo al que asociar el EVENT."\n            )\n        offset = self.EVENT_OFFSET_S if offset_s is None else max(0.0, float(offset_s))\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        frame_anchor = float(self._last_frame_anchor)\n        self._wait_until(frame_anchor + offset)\n\n        # IMPORTANT: this pulses RTS directly instead of set_frame(), so the\n        # original visual FRAME anchor remains unchanged for timing/logging.\n        high_at = self._event_pair_high()\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            low_at = self._event_pair_low()\n\n        return {\n            "frame_anchor_perf_s": frame_anchor,\n            "event_high_perf_s": high_at,\n            "event_low_perf_s": low_at,\n            "event_offset_s": max(0.0, high_at - frame_anchor),\n            "event_width_s": max(0.0, low_at - high_at),\n        }\n\n    def pulse_universal_event_after_frame(self, aux_state, offset_s=None, width_s=None):\n        """Pulse only UNIVERSAL ~3 ms after FRAME while setting AUX EVENT state.\n\n        This is the v1.27 experimental encoding used by the display worker:\n        RTS/UNIVERSAL keeps the validated short EVENT pulse, whereas TX/BREAK\n        becomes a held condition-state channel. The visual FRAME anchor is never\n        overwritten.\n        """\n        if self._last_frame_anchor is None:\n            raise RuntimeError(\n                "No hay un FRAME_SYNC previo al que asociar el EVENT."\n            )\n        offset = self.EVENT_OFFSET_S if offset_s is None else max(0.0, float(offset_s))\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        frame_anchor = float(self._last_frame_anchor)\n        desired_aux = 1 if int(aux_state) else 0\n        self._wait_until(frame_anchor + offset)\n\n        universal_high_at = None\n        aux_transition_at = None\n        try:\n            # UNIVERSAL receives the precise second pulse; use _set() directly so\n            # the FRAME anchor remains the original visual flip.\n            self._set("frame", 1)\n            universal_high_at = _ftdi_time.perf_counter()\n            # AUX EVENT is a held state, changed at the same condition onset.\n            self._set("event", desired_aux)\n            aux_transition_at = _ftdi_time.perf_counter()\n            self._event_high_at = aux_transition_at if desired_aux else None\n            self._wait_until(universal_high_at + width)\n        finally:\n            # Release ONLY UNIVERSAL. AUX EVENT deliberately remains at its\n            # requested condition state until the next onset or explicit reset.\n            self._set("frame", 0)\n            universal_low_at = _ftdi_time.perf_counter()\n\n        return {\n            "frame_anchor_perf_s": frame_anchor,\n            "event_high_perf_s": universal_high_at,\n            "event_low_perf_s": universal_low_at,\n            "event_offset_s": max(0.0, universal_high_at - frame_anchor),\n            "event_width_s": max(0.0, universal_low_at - universal_high_at),\n            "aux_transition_perf_s": aux_transition_at,\n            "aux_state": desired_aux,\n        }\n\n    def pulse_aux_marker(self, width_s=None):\n        """Emit a short marker only on AUX EVENT (TX/BREAK).\n\n        UNIVERSAL/RTS is deliberately untouched. This is used for protocol\n        pause markers in v1.28. AUX always returns LOW when the pulse ends.\n        """\n        width = self.EVENT_PULSE_MIN_S if width_s is None else max(\n            self.EVENT_PULSE_MIN_S, float(width_s)\n        )\n        # Start from a defined LOW state, then generate a clean HIGH pulse.\n        self._set("event", 0)\n        self._event_high_at = None\n        self._set("event", 1)\n        high_at = _ftdi_time.perf_counter()\n        self._event_high_at = high_at\n        try:\n            self._wait_until(high_at + width)\n        finally:\n            self._set("event", 0)\n            low_at = _ftdi_time.perf_counter()\n            self._event_high_at = None\n        return {\n            "aux_high_perf_s": high_at,\n            "aux_low_perf_s": low_at,\n            "aux_width_s": max(0.0, low_at - high_at),\n        }\n\n    def idle(self, strict=False):\n        errors = []\n        if self.serial is not None:\n            # Attempt BOTH lines even if one write fails.\n            for name, action in (("FRAME", self.set_frame), ("EVENT", self.set_event)):\n                try:\n                    action(0)\n                except Exception as exc:\n                    errors.append(f"{name}: {type(exc).__name__}: {exc}")\n        self._frame_high_at = None\n        self._last_frame_anchor = None\n        self._event_high_at = None\n        if errors and strict:\n            raise RuntimeError("; ".join(errors))\n        return errors\n\n    def close(self):\n        errors = self.idle()\n        ser, self.serial = self.serial, None\n        if ser is not None:\n            try:\n                ser.close()\n            except Exception as exc:\n                errors.append(f"Cerrar FTDI: {type(exc).__name__}: {exc}")\n        return errors\n\n\nimport csv\nimport json\nimport math\nimport mmap\nimport os\nimport sys\nimport time\nimport traceback\nfrom datetime import datetime\nfrom pathlib import Path\n\nimport numpy as np\n\nGRAY = (0.0, 0.0, 0.0)\nWHITE = (1.0, 1.0, 1.0)\nBLACK = (-1.0, -1.0, -1.0)\n\nstartup_config_path = Path(sys.argv[1])\ncommand_path = Path(sys.argv[2])\nstatus_path = Path(sys.argv[3])\nresult_path = Path(sys.argv[4])\nlog_dir = Path(sys.argv[5])\nabort_path = Path(sys.argv[6])\nprotocol_pause_path = (\n    abort_path.with_name(\n        "protocol_pause.json"\n    )\n)\n\nlog_dir.mkdir(parents=True, exist_ok=True)\n\ndef write_json_atomic(path, data):\n    """\n    Atomic JSON write with protection against short-lived Windows file\n    locks (WinError 5/32), which can occur when another process briefly\n    reads or scans the destination file.\n\n    status.json is operational telemetry, not stimulus data. If Windows\n    keeps it locked after all retries, the visual experiment must continue\n    safely rather than aborting because of a GUI/status-file race.\n    """\n    path = Path(path)\n\n    # A process-specific temporary name avoids collisions with a stale\n    # status.json.tmp left behind by a previous interrupted process.\n    tmp = path.with_name(\n        path.name\n        + f".{os.getpid()}.tmp"\n    )\n\n    payload = json.dumps(\n        data,\n        indent=2,\n        ensure_ascii=False\n    )\n\n    tmp.write_text(\n        payload,\n        encoding="utf-8"\n    )\n\n    last_exc = None\n\n    for attempt in range(30):\n        try:\n            os.replace(\n                tmp,\n                path\n            )\n            return True\n\n        except PermissionError as exc:\n            last_exc = exc\n            time.sleep(\n                0.01\n                + 0.002 * attempt\n            )\n\n        except OSError as exc:\n            # Common transient Windows sharing/access errors:\n            # 5 = ACCESS_DENIED, 32 = SHARING_VIOLATION.\n            if getattr(\n                exc,\n                "winerror",\n                None\n            ) in (\n                5,\n                32\n            ):\n                last_exc = exc\n                time.sleep(\n                    0.01\n                    + 0.002 * attempt\n                )\n            else:\n                raise\n\n    # Clean the temporary file when possible.\n    try:\n        if tmp.exists():\n            tmp.unlink()\n    except Exception:\n        pass\n\n    # A status-file lock must never stop a running stimulus/protocol.\n    try:\n        is_status_path = (\n            path.resolve()\n            == status_path.resolve()\n        )\n    except Exception:\n        is_status_path = (\n            str(path)\n            == str(status_path)\n        )\n\n    if is_status_path:\n        return False\n\n    if last_exc is not None:\n        raise last_exc\n\n    raise OSError(\n        f"No se pudo guardar JSON: {path}"\n    )\n\ndef seconds_to_frames(seconds, hz):\n    seconds = float(seconds)\n    if seconds <= 0:\n        return 0\n    return max(1, int(round(seconds * hz)))\n\ndef parse_float_list(text):\n    values = []\n    for part in str(text).replace(";", ",").split(","):\n        part = part.strip()\n        if part:\n            values.append(float(part))\n    return values\n\ndef rgb255_to_psychopy(rgb):\n    return tuple((float(v) / 127.5) - 1.0 for v in rgb)\n\ndef get_visual_size_deg(width_cm, height_cm, distance_cm):\n    width_deg = math.degrees(\n        2.0 * math.atan(width_cm / (2.0 * distance_cm))\n    )\n    height_deg = math.degrees(\n        2.0 * math.atan(height_cm / (2.0 * distance_cm))\n    )\n    return width_deg, height_deg\n\ndef colored_grating_texture(color_a, color_b, waveform, contrast):\n    """\n    One-cycle RGB texture. At contrast=1 the extrema are exactly the\n    selected colors. At contrast=0 both converge to their midpoint.\n    """\n    a = np.asarray(rgb255_to_psychopy(color_a), dtype=np.float32)\n    b = np.asarray(rgb255_to_psychopy(color_b), dtype=np.float32)\n    midpoint = (a + b) / 2.0\n\n    a_eff = midpoint + float(contrast) * (a - midpoint)\n    b_eff = midpoint + float(contrast) * (b - midpoint)\n\n    res = 256\n    phase = np.linspace(0.0, 2.0 * np.pi, res, endpoint=False)\n\n    if str(waveform).lower() == "cuadrado":\n        weight = (np.sin(phase) >= 0.0).astype(np.float32)\n    else:\n        weight = ((np.sin(phase) + 1.0) / 2.0).astype(np.float32)\n\n    line = (\n        b_eff[None, :]\n        + weight[:, None] * (a_eff - b_eff)[None, :]\n    )\n\n    texture = np.empty((res, res, 3), dtype=np.float32)\n    texture[:] = line[None, :, :]\n    return np.ascontiguousarray(texture)\n\n\ndef asymmetric_grating_texture(\n    color_a, color_b, contrast, polarity="A_TO_B", res=256\n):\n    """\n    One-cycle asymmetric RGB texture.\n\n    A_TO_B: Color A starts at the reset edge and follows a smooth half-cosine\n    transition to Color B. The periodic texture boundary then jumps abruptly\n    back to A. B_TO_A is the exact inverse polarity.\n\n    The profile is sinusoidal in digital RGB space (not gamma-linearized\n    luminance unless the display has been calibrated separately).\n    """\n    a = np.asarray(rgb255_to_psychopy(color_a), dtype=np.float32)\n    b = np.asarray(rgb255_to_psychopy(color_b), dtype=np.float32)\n    midpoint = (a + b) / 2.0\n\n    a_eff = midpoint + float(contrast) * (a - midpoint)\n    b_eff = midpoint + float(contrast) * (b - midpoint)\n\n    u = (\n        np.arange(int(res), dtype=np.float32)\n        / float(int(res))\n    )\n    # 1 at u=0, approaches 0 at u->1; wrap is the abrupt reset.\n    weight = 0.5 * (1.0 + np.cos(np.pi * u))\n    if str(polarity).strip().upper() in (\n        "B_TO_A",\n        "B→A",\n        "B-A",\n    ):\n        weight = 1.0 - weight\n\n    line = (\n        b_eff[None, :]\n        + weight[:, None] * (a_eff - b_eff)[None, :]\n    )\n\n    texture = np.empty((int(res), int(res), 3), dtype=np.float32)\n    texture[:] = line[None, :, :]\n    return np.ascontiguousarray(texture)\n\n\ndef gaussian_mask_deg(size_deg, sigma_deg, res=256):\n    """\n    PsychoPy custom mask in the documented -1..1 range.\n    The Gaussian sigma is expressed in visual degrees.\n    """\n    size_deg = float(size_deg)\n    sigma_deg = float(sigma_deg)\n\n    coords = np.linspace(\n        -size_deg / 2.0,\n        size_deg / 2.0,\n        int(res),\n        endpoint=False,\n        dtype=np.float32\n    )\n    # Center samples in their bins.\n    coords += (\n        size_deg\n        / (2.0 * float(res))\n    )\n\n    xx, yy = np.meshgrid(\n        coords,\n        coords\n    )\n    alpha = np.exp(\n        -(\n            xx * xx\n            + yy * yy\n        )\n        / (\n            2.0\n            * sigma_deg\n            * sigma_deg\n        )\n    ).astype(\n        np.float32\n    )\n\n    mask = (\n        2.0 * alpha\n        - 1.0\n    )\n    return np.ascontiguousarray(\n        mask,\n        dtype=np.float32\n    )\n\nwin = None\nabort_file = None\nabort_map = None\n\ntry:\n    from psychopy import visual, monitors, event, core\n\n    startup = json.loads(\n        startup_config_path.read_text(encoding="utf-8")\n    )\n\n    screen_index = int(startup["screen_index"])\n    width_px = int(startup["screen_width_px"])\n    height_px = int(startup["screen_height_px"])\n    screen_x = int(startup.get("screen_x", 0))\n    screen_y = int(startup.get("screen_y", 0))\n    physical_width_cm = float(startup["physical_width_cm"])\n    physical_height_cm = float(startup["physical_height_cm"])\n    distance_cm = float(startup["distance_cm"])\n    GRAY = rgb255_to_psychopy(startup.get("adaptation_rgb", [128, 128, 128]))\n    IDLE_COLOR = rgb255_to_psychopy(startup.get("idle_rgb", [0, 0, 0]))\n\n    abort_file = abort_path.open("r+b")\n    abort_map = mmap.mmap(abort_file.fileno(), 1)\n\n    write_json_atomic(status_path, {\n        "state": "opening",\n        "message": "Abriendo pantalla PsychoPy..."\n    })\n\n    mon = monitors.Monitor(\n        name="MEA_Persistent_Stimulus_Display",\n        width=physical_width_cm,\n        distance=distance_cm\n    )\n    mon.setSizePix((width_px, height_px))\n\n    def keep_psychopy_on_stimulus_monitor():\n        """Keep PsychoPy above the safe background layer without stealing focus."""\n        try:\n            import ctypes\n            user32 = ctypes.windll.user32\n            user32.SetWindowPos.argtypes = [\n                ctypes.c_void_p, ctypes.c_void_p,\n                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,\n                ctypes.c_uint\n            ]\n            user32.SetWindowPos.restype = ctypes.c_int\n\n            hwnd_value = getattr(win.winHandle, "_hwnd", None)\n            if hwnd_value is None:\n                return\n\n            HWND_TOPMOST = ctypes.c_void_p(-1 & 0xFFFFFFFFFFFFFFFF)\n            SWP_NOMOVE = 0x0002\n            SWP_NOSIZE = 0x0001\n            SWP_NOACTIVATE = 0x0010\n            SWP_SHOWWINDOW = 0x0040\n\n            user32.SetWindowPos(\n                ctypes.c_void_p(hwnd_value),\n                HWND_TOPMOST,\n                0, 0, 0, 0,\n                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW\n            )\n        except Exception:\n            pass\n\n    win = visual.Window(\n        size=(width_px, height_px),\n        screen=screen_index,\n        fullscr=True,\n        allowGUI=False,\n        color=IDLE_COLOR,\n        colorSpace="rgb",\n        units="deg",\n        monitor=mon,\n        waitBlanking=True,\n        checkTiming=True,\n        winType="pyglet",\n        autoLog=False\n    )\n\n    # Keep the experimental window above the safety layer even when the\n    # operator clicks the GUI on the other monitor. Do not steal focus.\n    keep_psychopy_on_stimulus_monitor()\n\n    width_deg, height_deg = get_visual_size_deg(\n        physical_width_cm, physical_height_cm, distance_cm\n    )\n    cover_size = 2.2 * math.hypot(width_deg, height_deg)\n\n    fullfield = visual.Rect(\n        win=win,\n        width=cover_size,\n        height=cover_size,\n        units="deg",\n        fillColor=IDLE_COLOR,\n        lineColor=IDLE_COLOR,\n        colorSpace="rgb",\n        pos=(0, 0)\n    )\n\n    def show_solid(color, do_flip=True):\n        fullfield.fillColor = color\n        fullfield.lineColor = color\n        fullfield.draw()\n        if do_flip:\n            return win.flip()\n        return None\n\n    # Start and remain on the safe idle background (black by default).\n    for _ in range(30):\n        show_solid(IDLE_COLOR)\n\n    write_json_atomic(status_path, {\n        "state": "measuring",\n        "message": "Midiendo refresco de forma silenciosa..."\n    })\n\n    # v1.32: robust refresh calibration from the actual PsychoPy flips.\n    # getActualFrameRate() can report a spurious rate on mixed-refresh\n    # multi-monitor Windows systems (e.g. ~91 Hz while the stimulus screen\n    # is physically flipping at ~60 Hz).  Time the real blocking win.flip()\n    # calls with perf_counter instead, reject outliers, and use the median\n    # interval as the experimental frame period.\n    def measure_refresh_from_real_flips(\n        warmup_frames=45,\n        sample_frames=180\n    ):\n        for _ in range(int(warmup_frames)):\n            show_solid(IDLE_COLOR)\n\n        intervals = []\n        last = time.perf_counter()\n        for _ in range(int(sample_frames)):\n            show_solid(IDLE_COLOR)\n            now_perf = time.perf_counter()\n            dt = now_perf - last\n            last = now_perf\n            if math.isfinite(dt) and dt > 0:\n                intervals.append(float(dt))\n\n        if len(intervals) < 30:\n            raise RuntimeError(\n                "No se pudieron obtener suficientes intervalos de refresco reales."\n            )\n\n        arr = np.asarray(intervals, dtype=np.float64)\n        median0 = float(np.median(arr))\n        if not math.isfinite(median0) or median0 <= 0:\n            raise RuntimeError(\n                "La medición monotónica del refresco no produjo un intervalo válido."\n            )\n\n        # First remove gross half/double-frame outliers. Then use MAD for\n        # transient OS scheduling excursions without biasing the central rate.\n        coarse = arr[(arr >= 0.55 * median0) & (arr <= 1.80 * median0)]\n        if coarse.size < 20:\n            coarse = arr\n        med = float(np.median(coarse))\n        abs_dev = np.abs(coarse - med)\n        mad = float(np.median(abs_dev))\n        if mad > 0:\n            robust_sigma = 1.4826 * mad\n            fine = coarse[abs_dev <= max(4.0 * robust_sigma, 0.00035)]\n            if fine.size >= 20:\n                coarse = fine\n\n        interval_s = float(np.median(coarse))\n        hz_value = 1.0 / interval_s\n        spread_ms = 1000.0 * float(np.percentile(coarse, 95) - np.percentile(coarse, 5))\n\n        if (\n            not math.isfinite(hz_value)\n            or hz_value < 20.0\n            or hz_value > 500.0\n        ):\n            raise RuntimeError(\n                f"Frecuencia de refresco real no plausible: {hz_value:.3f} Hz."\n            )\n\n        return {\n            "hz": float(hz_value),\n            "interval_s": float(interval_s),\n            "n_raw": int(arr.size),\n            "n_used": int(coarse.size),\n            "p05_p95_spread_ms": float(spread_ms),\n        }\n\n    refresh_measurement = measure_refresh_from_real_flips()\n    hz = float(refresh_measurement["hz"])\n    expected_interval = float(refresh_measurement["interval_s"])\n\n    # Return to the safe idle image after the silent calibration.\n    show_solid(IDLE_COLOR)\n\n    write_json_atomic(status_path, {\n        "state": "ready",\n        "message": "Pantalla experimental estable · espera segura",\n        "hz": hz,\n        "screen_index": screen_index,\n        "resolution": [width_px, height_px]\n    })\n\n    last_command_id = None\n    last_idle_flip = time.perf_counter()\n\n    while True:\n        now = time.perf_counter()\n\n        # Refresh the safe idle image periodically to keep the pyglet\n        # window responsive without changing visual content.\n        if now - last_idle_flip >= 0.25:\n            show_solid(IDLE_COLOR)\n            keep_psychopy_on_stimulus_monitor()\n            last_idle_flip = now\n\n        command = None\n        if command_path.exists():\n            try:\n                command = json.loads(\n                    command_path.read_text(encoding="utf-8")\n                )\n            except Exception:\n                command = None\n\n        if not command or command.get("command_id") == last_command_id:\n            core.wait(0.01)\n            continue\n\n        command_id = command.get("command_id")\n        action = command.get("action")\n\n        if action == "shutdown":\n            show_solid(IDLE_COLOR)\n            break\n\n        if action == "set_idle_color":\n            try:\n                IDLE_COLOR = rgb255_to_psychopy(command.get("rgb", [0, 0, 0]))\n                show_solid(IDLE_COLOR)\n                keep_psychopy_on_stimulus_monitor()\n                write_json_atomic(status_path, {\n                    "state": "ready",\n                    "message": "Pantalla experimental estable · espera segura",\n                    "hz": hz,\n                    "screen_index": screen_index,\n                    "resolution": [width_px, height_px],\n                    "last_command_id": command_id,\n                })\n            finally:\n                last_command_id = command_id\n            core.wait(0.005)\n            continue\n\n        if action != "run":\n            last_command_id = command_id\n            core.wait(0.01)\n            continue\n\n        last_command_id = command_id\n        cfg = command["config"]\n        # v1.22: adaptation background is a run-level experimental setting;\n        # safe idle remains black; operator dark-room mode never changes this display.\n        GRAY = rgb255_to_psychopy(cfg.get("adaptation_rgb", [128, 128, 128]))\n        IDLE_COLOR = rgb255_to_psychopy(cfg.get("idle_rgb", [0, 0, 0]))\n        stimulus_type = cfg["stimulus_type"]\n        command_stimulus_type = stimulus_type\n\n        safe_run_prefix = stimulus_type\n        if stimulus_type == "protocol":\n            safe_protocol_name = "".join(\n                c\n                if (\n                    c.isalnum()\n                    or c in "-_"\n                )\n                else "_"\n                for c in str(\n                    cfg.get(\n                        "protocol_name",\n                        "protocol"\n                    )\n                )\n            ).strip("_")[:60]\n\n            if not safe_protocol_name:\n                safe_protocol_name = "protocol"\n\n            safe_run_prefix = (\n                f"protocol_"\n                f"{safe_protocol_name}_"\n                f"v{cfg.get(\'protocol_version\', 1)}"\n            )\n\n        run_stamp = datetime.now().strftime(\n            "%Y%m%d_%H%M%S"\n        )\n\n        # Historical base retained for filenames so CSV/TTL/JSON names\n        # remain fully compatible with previous versions.\n        run_base_name = (\n            f"{safe_run_prefix}_{run_stamp}"\n        )\n\n        # Date/time FIRST only for the containing run folder. This makes\n        # Windows Explorer sort experiment folders chronologically by name:\n        # YYYYMMDD_HHMMSS_stimulus-or-protocol\n        run_folder_name = (\n            f"{run_stamp}_{safe_run_prefix}"\n        )\n        run_dir = log_dir / run_folder_name\n        run_dir.mkdir(\n            parents=True,\n            exist_ok=True\n        )\n\n        # Clear abort flag without recreating the experimental window.\n        abort_map.seek(0)\n        abort_map.write(b"\\x00")\n        abort_map.flush()\n\n        # Geometry may be edited by the operator between stimuli.\n        physical_width_cm = float(cfg["physical_width_cm"])\n        physical_height_cm = float(cfg["physical_height_cm"])\n        distance_cm = float(cfg["distance_cm"])\n\n        try:\n            win.monitor.setWidth(physical_width_cm)\n            win.monitor.setDistance(distance_cm)\n            win.monitor.setSizePix((width_px, height_px))\n        except Exception:\n            pass\n\n        width_deg, height_deg = get_visual_size_deg(\n            physical_width_cm, physical_height_cm, distance_cm\n        )\n        cover_size = 2.2 * math.hypot(width_deg, height_deg)\n        fullfield.width = cover_size\n        fullfield.height = cover_size\n\n        keep_psychopy_on_stimulus_monitor()\n\n        write_json_atomic(status_path, {\n            "state": "running",\n            "message": f"Ejecutando {stimulus_type}...",\n            "hz": hz,\n            "command_id": command_id\n        })\n\n        rows = []\n        previous_flip = None\n        global_frame = 0\n        possible_drops = 0\n        details = {}\n\n        # v1.32: the absolute monotonic pacer is now driven by a refresh\n        # period measured directly from real win.flip() calls on this exact\n        # stimulus monitor. This avoids the erroneous getActualFrameRate()\n        # estimates seen on mixed-refresh multi-monitor Windows systems.\n        # Frame-locked stimuli keep their deterministic frame sequence while\n        # durations, speeds and temporal frequencies are referenced to the\n        # empirically observed display cadence.\n        pacing_anchor_perf = None\n        pacing_anchor_frame = 0\n        pacing_wait_total_s = 0.0\n        pacing_late_frame_count = 0\n        pacing_max_lateness_s = 0.0\n\n        # Prepared resources are used only by Protocol Builder runs.\n        # They are created before the first logged protocol frame so\n        # expensive PsychoPy/OpenGL object construction cannot interrupt\n        # an already-running timed sequence.\n        prepared_step = None\n        protocol_prepared_steps = {}\n\n        # Protocol context is blank for ordinary single-stimulus runs.\n        # flip_and_log() and ttl_commit() read these values so a protocol\n        # can remain one continuous visual/TTL record.\n        protocol_name_context = ""\n        protocol_version_context = ""\n        protocol_rep_context = ""\n        protocol_step_index_context = ""\n        protocol_step_name_context = ""\n        protocol_step_kind_context = ""\n\n        # ----------------------------------------------------------\n        # Trigger layer — v1.29.\n        #\n        # IMPORTANT: MaxOne Legacy FTDI preserves the v1.17 behaviour\n        # unchanged: FRAME_SYNC on every logged flip and EVENT HIGH for one\n        # complete display frame at each principal-condition onset.\n        #\n        # Trigger Box TTL genérica is an additional, independent mode:\n        # FRAME_SYNC remains attached to the real PsychoPy flip and UNIVERSAL\n        # receives a second >=1 ms EVENT pulse ~3 ms later. AUX EVENT is held\n        # as a readable condition-state channel; protocol pauses add an AUX-only short marker (v1.28).\n        ttl_mode = str(cfg.get("ttl_mode", "simulation")).strip().lower()\n        ttl_pin = int(cfg.get("ttl_pin", 8))\n        ttl_port_requested = str(cfg.get("ttl_port", "auto")).strip() or "auto"\n        ttl_port_resolved = ""\n        ttl_backend = None\n        trigger_backend_fault = ""\n        trigger_stopping = False\n        frame_sync_pulses = 0\n\n        # Legacy/simulation state (kept from v1.17).\n        ttl_state = 0\n        ttl_requested_state = 0\n        ttl_events = []\n        ttl_event_counter = 0\n        ttl_pulse_pending = None\n        ttl_pulse_width_frames = 1\n\n        # Generic trigger-box state (v1.28). UNIVERSAL preserves the validated\n        # short EVENT pulse. AUX EVENT becomes a human-readable held state:\n        # ON/OFF => ON HIGH / OFF LOW; all other principal conditions alternate.\n        generic_event_pending = None\n        generic_aux_state = 0\n        event_offset_requested_s = GenericTTLFTDI.EVENT_OFFSET_S\n        event_pulse_width_s = GenericTTLFTDI.EVENT_PULSE_MIN_S\n        # v1.28: protocol pauses receive one short HIGH marker on AUX EVENT.\n        # Use the same validated minimum width as the former EVENT pulses.\n        pause_marker_width_s = GenericTTLFTDI.EVENT_PULSE_MIN_S\n\n\n        def _trigger_write(action, *args):\n            """Latch I/O errors without interrupting PsychoPy\'s callback queue.\n\n            flip_and_log/check_abort raises outside callOnFlip, so pending callbacks\n            cannot be replayed by the next safety flip after a failed port write.\n            """\n            global trigger_backend_fault\n            try:\n                action(*args)\n                return True\n            except Exception as exc:\n                if not trigger_backend_fault:\n                    trigger_backend_fault = f"{type(exc).__name__}: {exc}"\n                if ttl_backend is not None:\n                    ttl_backend.idle()\n                return False\n\n\n        def check_trigger_error():\n            if trigger_backend_fault:\n                if ttl_mode == "maxone_ftdi":\n                    raise RuntimeError(\n                        f"Error del generador MaxOne FTDI: {trigger_backend_fault}"\n                    )\n                raise RuntimeError(\n                    f"Error de la Trigger Box TTL genérica: {trigger_backend_fault}"\n                )\n\n\n        def open_trigger_backend():\n            global ttl_backend, ttl_port_resolved\n            if ttl_mode not in (\n                "simulation", "disabled", "maxone_ftdi", "generic_ftdi"\n            ):\n                raise ValueError(f"Modo de trigger no soportado: {ttl_mode}")\n            if ttl_mode == "maxone_ftdi":\n                # Legacy path: exactly the v1.17 backend.\n                ttl_backend = MaxOneFTDI(ttl_port_requested)\n                ttl_port_resolved = ttl_backend.open()\n            elif ttl_mode == "generic_ftdi":\n                ttl_backend = GenericTTLFTDI(ttl_port_requested)\n                ttl_port_resolved = ttl_backend.open()\n\n\n        def close_trigger_backend():\n            global ttl_backend, trigger_backend_fault\n            backend, ttl_backend = ttl_backend, None\n            if backend is not None:\n                errors = backend.close()\n                if errors and not trigger_backend_fault:\n                    trigger_backend_fault = "; ".join(errors)\n\n\n        def frame_sync_high():\n            global frame_sync_pulses\n            if ttl_backend is not None and not trigger_stopping and not trigger_backend_fault:\n                if _trigger_write(ttl_backend.frame_high):\n                    frame_sync_pulses += 1\n\n\n        def frame_sync_low():\n            if ttl_backend is not None:\n                _trigger_write(ttl_backend.frame_low)\n\n\n        def finish_trigger_run(reason, suppress_errors=False):\n            """Make pending onset callbacks harmless and always release the port."""\n            global trigger_stopping, ttl_pulse_pending, generic_event_pending\n            global trigger_backend_fault\n            # Before stopping callbacks, guarantee AUX EVENT is physically LOW.\n            # This is independent of UNIVERSAL and is logged when it changes.\n            if ttl_mode == "generic_ftdi":\n                force_generic_aux_low(reason)\n            trigger_stopping = True\n            ttl_pulse_pending = None\n            generic_event_pending = None\n            errors = []\n            # Preserve the v1.17 MaxOne/simulation falling-edge behaviour.\n            if ttl_backend is not None and suppress_errors:\n                errors.extend(ttl_backend.idle())\n            try:\n                if (\n                    ttl_mode in ("simulation", "maxone_ftdi")\n                    and ttl_requested_state != 0\n                ):\n                    queue_ttl(0, reason, 0, stimulus_type)\n                safe_gray_flip_and_stamp()\n            except Exception as exc:\n                errors.append(f"Retorno a gris: {type(exc).__name__}: {exc}")\n            finally:\n                close_trigger_backend()\n            if errors and not trigger_backend_fault:\n                trigger_backend_fault = "; ".join(errors)\n            if not suppress_errors:\n                check_trigger_error()\n\n\n        # ----------------------------------------------------------\n        # Legacy EVENT path — copied from v1.17 semantics.\n        # ----------------------------------------------------------\n        def ttl_commit(\n            state, event_name, trial,\n            condition, scheduled_frame\n        ):\n            global ttl_state, ttl_event_counter\n\n            state = int(state)\n            if trigger_stopping and state:\n                return\n            if ttl_backend is not None:\n                if not _trigger_write(ttl_backend.set_event, state):\n                    return\n\n            ttl_state = state\n            ttl_event_counter += 1\n\n            ttl_events.append({\n                "event_index": ttl_event_counter,\n                "state": ttl_state,\n                "level": "HIGH" if ttl_state else "LOW",\n                "event_name": str(event_name),\n                "trial": trial,\n                "condition": str(condition),\n                "scheduled_frame": int(scheduled_frame),\n                "callback_time_s": f"{core.getTime():.9f}",\n                "flip_time_s": "",\n                "mode": ttl_mode,\n                "pin": (0 if ttl_mode == "maxone_ftdi" else ttl_pin),\n                "port": ttl_port_resolved,\n                "signal": (\n                    "EVENT_TX_BREAK"\n                    if ttl_mode == "maxone_ftdi"\n                    else "EVENT_SIMULATED"\n                ),\n                "maxone_bit": (0 if ttl_mode == "maxone_ftdi" else ""),\n                "event_role": "CONDITION_ONSET" if ttl_state else "PULSE_END",\n                "pulse_width_frames": ttl_pulse_width_frames,\n                "event_offset_requested_ms": "",\n                "pulse_width_requested_ms": "",\n                "frame_anchor_perf_s": "",\n                "transition_perf_s": "",\n                "event_offset_measured_ms": "",\n                "pulse_width_measured_ms": "",\n                "universal_code": "",\n                "aux_event_state": "",\n                "aux_event_level": "",\n                "aux_event_policy": "",\n                "universal_pulse_phase": "",\n                "protocol_name": protocol_name_context,\n                "protocol_version": protocol_version_context,\n                "protocol_rep": protocol_rep_context,\n                "protocol_step_index": protocol_step_index_context,\n                "protocol_step_name": protocol_step_name_context,\n                "protocol_step_kind": protocol_step_kind_context,\n            })\n\n\n        def queue_ttl(\n            state, event_name,\n            trial=0, condition=""\n        ):\n            global ttl_requested_state\n\n            if ttl_mode not in (\n                "simulation",\n                "maxone_ftdi"\n            ):\n                return\n\n            state = 1 if int(state) else 0\n            if state == ttl_requested_state:\n                return\n\n            ttl_requested_state = state\n            scheduled_frame = global_frame + 1\n\n            # EVENT transition is attached to the exact next PsychoPy flip.\n            win.callOnFlip(\n                ttl_commit,\n                state,\n                event_name,\n                trial,\n                condition,\n                scheduled_frame\n            )\n\n\n        def queue_ttl_event(\n            event_name, trial=0, condition=""\n        ):\n            """Mark the onset of one principal condition."""\n            global ttl_pulse_pending, generic_event_pending\n\n            if ttl_mode == "generic_ftdi":\n                if generic_event_pending is not None:\n                    raise ValueError(\n                        "Se han solicitado dos EVENT antes del mismo frame visual. "\n                        "Cada condición principal debe tener un onset diferenciable."\n                    )\n                generic_event_pending = {\n                    "event_name": str(event_name),\n                    "trial": trial,\n                    "condition": str(condition),\n                    "scheduled_frame": int(global_frame + 1),\n                    "protocol_name": protocol_name_context,\n                    "protocol_version": protocol_version_context,\n                    "protocol_rep": protocol_rep_context,\n                    "protocol_step_index": protocol_step_index_context,\n                    "protocol_step_name": protocol_step_name_context,\n                    "protocol_step_kind": protocol_step_kind_context,\n                }\n                return\n\n            # v1.17 behaviour for Simulación and MaxOne Legacy FTDI.\n            if ttl_mode not in (\n                "simulation",\n                "maxone_ftdi"\n            ):\n                return\n\n            if ttl_state != 0 or ttl_requested_state != 0:\n                raise ValueError(\n                    "Dos condiciones principales están demasiado juntas "\n                    "para generar pulsos EVENT separados. Cada condición "\n                    "debe durar al menos 2 frames."\n                )\n\n            ttl_pulse_pending = (\n                str(event_name),\n                trial,\n                str(condition)\n            )\n            queue_ttl(\n                1,\n                str(event_name),\n                trial,\n                str(condition)\n            )\n\n\n        def finish_ttl_pulse_after_flip():\n            """\n            After the first HIGH frame has been displayed, queue EVENT LOW\n            for the immediately following display flip.\n            """\n            global ttl_pulse_pending\n\n            if (\n                ttl_mode in (\n                    "simulation",\n                    "maxone_ftdi"\n                )\n                and ttl_pulse_pending is not None\n                and ttl_state == 1\n                and ttl_requested_state == 1\n            ):\n                event_name, trial, condition = ttl_pulse_pending\n                ttl_pulse_pending = None\n                queue_ttl(\n                    0,\n                    f"{event_name}_PULSE_END",\n                    trial,\n                    condition\n                )\n\n\n        # ----------------------------------------------------------\n        # Generic trigger-box EVENT + AUX state + pause-marker path — v1.28.\n        # ----------------------------------------------------------\n        def _generic_aux_target(event_name):\n            """Return (state, policy) for the readable AUX EVENT condition track."""\n            name = str(event_name).strip().upper()\n            if name == "ON_START":\n                return 1, "ON_OFF_STATE"\n            if name == "OFF_START":\n                return 0, "ON_OFF_STATE"\n            # For moving bars, gratings, RF positions, noise/temporal blocks, etc.,\n            # every principal condition onset creates an unmistakable edge.\n            return (0 if generic_aux_state else 1), "ALTERNATE_BY_CONDITION"\n\n\n        def _append_generic_event_transition(\n            pending, state, role, flip_time, aux_state, aux_policy, pulse_phase,\n            transition_perf_s="", metrics=None\n        ):\n            global ttl_event_counter\n            ttl_event_counter += 1\n            metrics = metrics or {}\n            measured_offset = metrics.get("event_offset_s", "")\n            measured_width = metrics.get("event_width_s", "")\n            frame_anchor = metrics.get("frame_anchor_perf_s", "")\n\n            ttl_events.append({\n                "event_index": ttl_event_counter,\n                # state/level preserve the validated UNIVERSAL event-pulse log.\n                "state": int(state),\n                "level": "HIGH" if state else "LOW",\n                "event_name": (\n                    pending["event_name"]\n                    if state else f\'{pending["event_name"]}_PULSE_END\'\n                ),\n                "trial": pending["trial"],\n                "condition": pending["condition"],\n                "scheduled_frame": pending["scheduled_frame"],\n                "callback_time_s": f"{core.getTime():.9f}",\n                "flip_time_s": (\n                    "" if flip_time is None else f"{float(flip_time):.9f}"\n                ),\n                "mode": ttl_mode,\n                "pin": 0,\n                "port": ttl_port_resolved,\n                "signal": "UNIVERSAL_RTS_EVENT_PULSE + AUX_EVENT_TX_BREAK_STATE",\n                "maxone_bit": "",\n                "event_role": role,\n                "pulse_width_frames": 0,\n                "event_offset_requested_ms":\n                    f"{1000.0 * event_offset_requested_s:.3f}",\n                "pulse_width_requested_ms":\n                    f"{1000.0 * event_pulse_width_s:.3f}",\n                "frame_anchor_perf_s": (\n                    "" if frame_anchor == "" else f"{float(frame_anchor):.9f}"\n                ),\n                "transition_perf_s": (\n                    "" if transition_perf_s == "" else f"{float(transition_perf_s):.9f}"\n                ),\n                "event_offset_measured_ms": (\n                    "" if measured_offset == ""\n                    else f"{1000.0 * float(measured_offset):.6f}"\n                ),\n                "pulse_width_measured_ms": (\n                    "" if measured_width == ""\n                    else f"{1000.0 * float(measured_width):.6f}"\n                ),\n                "universal_code": "SECOND_PULSE_AFTER_FRAME",\n                "aux_event_state": int(aux_state),\n                "aux_event_level": "HIGH" if aux_state else "LOW",\n                "aux_event_policy": str(aux_policy),\n                "universal_pulse_phase": str(pulse_phase),\n                "protocol_name": pending["protocol_name"],\n                "protocol_version": pending["protocol_version"],\n                "protocol_rep": pending["protocol_rep"],\n                "protocol_step_index": pending["protocol_step_index"],\n                "protocol_step_name": pending["protocol_step_name"],\n                "protocol_step_kind": pending["protocol_step_kind"],\n            })\n\n\n        def _append_generic_aux_reset(reason, previous_state, transition_perf_s=""):\n            """Log the explicit end-of-stimulus AUX return to LOW."""\n            global ttl_event_counter\n            ttl_event_counter += 1\n            ttl_events.append({\n                "event_index": ttl_event_counter,\n                "state": 0,\n                "level": "LOW",\n                "event_name": str(reason),\n                "trial": 0,\n                "condition": "AUX_RESET_LOW",\n                "scheduled_frame": int(global_frame),\n                "callback_time_s": f"{core.getTime():.9f}",\n                "flip_time_s": "",\n                "mode": ttl_mode,\n                "pin": 0,\n                "port": ttl_port_resolved,\n                "signal": "AUX_EVENT_TX_BREAK_STATE",\n                "maxone_bit": "",\n                "event_role": "AUX_RESET_LOW",\n                "pulse_width_frames": 0,\n                "event_offset_requested_ms": "",\n                "pulse_width_requested_ms": "",\n                "frame_anchor_perf_s": "",\n                "transition_perf_s": (\n                    "" if transition_perf_s == "" else f"{float(transition_perf_s):.9f}"\n                ),\n                "event_offset_measured_ms": "",\n                "pulse_width_measured_ms": "",\n                "universal_code": "",\n                "aux_event_state": 0,\n                "aux_event_level": "LOW",\n                "aux_event_policy": "FORCED_END_RESET",\n                "universal_pulse_phase": "",\n                "protocol_name": protocol_name_context,\n                "protocol_version": protocol_version_context,\n                "protocol_rep": protocol_rep_context,\n                "protocol_step_index": protocol_step_index_context,\n                "protocol_step_name": protocol_step_name_context,\n                "protocol_step_kind": protocol_step_kind_context,\n            })\n\n\n        def force_generic_aux_low(reason):\n            """Force AUX EVENT LOW without adding an EVENT pulse to UNIVERSAL."""\n            global generic_aux_state\n            if ttl_mode != "generic_ftdi":\n                return\n            previous_state = int(generic_aux_state)\n            transition_perf = ""\n            if ttl_backend is not None:\n                if _trigger_write(ttl_backend.set_event, 0):\n                    transition_perf = _ftdi_time.perf_counter()\n            generic_aux_state = 0\n            if previous_state:\n                _append_generic_aux_reset(\n                    reason, previous_state, transition_perf_s=transition_perf\n                )\n\n\n        def _append_generic_pause_marker(\n            state, transition_perf_s="", measured_width_s="", flip_time_s=""\n        ):\n            """Log the AUX-only short pulse marking the start of a protocol pause."""\n            global ttl_event_counter\n            ttl_event_counter += 1\n            ttl_events.append({\n                "event_index": ttl_event_counter,\n                "state": int(state),\n                "level": "HIGH" if state else "LOW",\n                "event_name": (\n                    "PROTOCOL_PAUSE_START" if state\n                    else "PROTOCOL_PAUSE_MARKER_END"\n                ),\n                "trial": 0,\n                "condition": "PAUSE_MARKER",\n                "scheduled_frame": int(global_frame),\n                "callback_time_s": f"{core.getTime():.9f}",\n                "flip_time_s": str(flip_time_s or ""),\n                "mode": ttl_mode,\n                "pin": 0,\n                "port": ttl_port_resolved,\n                "signal": "AUX_EVENT_TX_BREAK_PAUSE_MARKER",\n                "maxone_bit": "",\n                "event_role": (\n                    "PAUSE_MARKER_START" if state else "PAUSE_MARKER_END"\n                ),\n                "pulse_width_frames": 0,\n                "event_offset_requested_ms": "",\n                "pulse_width_requested_ms": f"{1000.0 * pause_marker_width_s:.3f}",\n                "frame_anchor_perf_s": "",\n                "transition_perf_s": (\n                    "" if transition_perf_s == ""\n                    else f"{float(transition_perf_s):.9f}"\n                ),\n                "event_offset_measured_ms": "",\n                "pulse_width_measured_ms": (\n                    "" if measured_width_s == ""\n                    else f"{1000.0 * float(measured_width_s):.6f}"\n                ),\n                "universal_code": "",\n                "aux_event_state": int(state),\n                "aux_event_level": "HIGH" if state else "LOW",\n                "aux_event_policy": "PAUSE_MARKER_PULSE",\n                "universal_pulse_phase": "",\n                "protocol_name": protocol_name_context,\n                "protocol_version": protocol_version_context,\n                "protocol_rep": protocol_rep_context,\n                "protocol_step_index": protocol_step_index_context,\n                "protocol_step_name": protocol_step_name_context,\n                "protocol_step_kind": protocol_step_kind_context,\n            })\n\n\n        def emit_generic_pause_marker():\n            """Mark protocol-pause onset with one short AUX HIGH pulse.\n\n            The first pause frame is already on screen when this function runs.\n            UNIVERSAL is never touched, and AUX is guaranteed LOW afterwards.\n            """\n            global generic_aux_state\n            if ttl_mode != "generic_ftdi":\n                return False\n\n            # A pause always starts from the unambiguous AUX baseline.\n            force_generic_aux_low("PROTOCOL_PAUSE_PRE_MARKER_LOW")\n            metrics = {}\n            if ttl_backend is not None:\n                try:\n                    metrics = ttl_backend.pulse_aux_marker(\n                        width_s=pause_marker_width_s\n                    )\n                except Exception as exc:\n                    if not trigger_backend_fault:\n                        _trigger_write(lambda: (_ for _ in ()).throw(exc))\n                    generic_aux_state = 0\n                    return False\n\n            generic_aux_state = 0\n            high_at = metrics.get("aux_high_perf_s", "")\n            low_at = metrics.get("aux_low_perf_s", "")\n            measured_width = metrics.get("aux_width_s", "")\n            first_pause_flip = ""\n            if rows:\n                last_row = rows[-1]\n                if (\n                    last_row.get("stimulus_type") == "protocol_pause"\n                    and int(last_row.get("global_frame", -1)) == int(global_frame)\n                ):\n                    first_pause_flip = last_row.get("flip_time_s", "")\n\n            _append_generic_pause_marker(\n                1, transition_perf_s=high_at,\n                measured_width_s=measured_width, flip_time_s=first_pause_flip\n            )\n            _append_generic_pause_marker(\n                0, transition_perf_s=low_at,\n                measured_width_s=measured_width, flip_time_s=first_pause_flip\n            )\n            return True\n\n\n        def emit_generic_event_for_current_frame(flip_time):\n            """Emit the validated UNIVERSAL EVENT pulse and update held AUX state."""\n            global generic_event_pending, ttl_state, generic_aux_state\n\n            if ttl_mode != "generic_ftdi":\n                return False\n            pending = generic_event_pending\n            if pending is None:\n                ttl_state = 0\n                return False\n\n            scheduled = int(pending["scheduled_frame"])\n            if scheduled > global_frame:\n                ttl_state = 0\n                return False\n            if scheduled < global_frame:\n                generic_event_pending = None\n                raise RuntimeError(\n                    "EVENT genérico pendiente fuera de sincronía con el frame visual."\n                )\n\n            generic_event_pending = None\n            if trigger_stopping:\n                ttl_state = 0\n                return False\n\n            aux_target, aux_policy = _generic_aux_target(pending["event_name"])\n            metrics = {}\n            if ttl_backend is not None:\n                try:\n                    metrics = ttl_backend.pulse_universal_event_after_frame(\n                        aux_state=aux_target,\n                        offset_s=event_offset_requested_s,\n                        width_s=event_pulse_width_s,\n                    )\n                except Exception as exc:\n                    if not trigger_backend_fault:\n                        _trigger_write(lambda: (_ for _ in ()).throw(exc))\n                    ttl_state = 0\n                    return False\n\n            # Simulation/no-backend still tracks the intended AUX state in logs.\n            generic_aux_state = int(aux_target)\n            high_perf = metrics.get("event_high_perf_s", "")\n            low_perf = metrics.get("event_low_perf_s", "")\n            ttl_state = 1\n            _append_generic_event_transition(\n                pending, 1, "CONDITION_ONSET", flip_time,\n                generic_aux_state, aux_policy, "START",\n                transition_perf_s=high_perf, metrics=metrics\n            )\n            _append_generic_event_transition(\n                pending, 0, "PULSE_END", flip_time,\n                generic_aux_state, aux_policy, "END",\n                transition_perf_s=low_perf, metrics=metrics\n            )\n            ttl_state = 0\n            return True\n\n\n        def safe_gray_flip_and_stamp():\n            """\n            Return the experimental display to the configured adaptation background.\n\n            Legacy MaxOne/simulation retains the v1.17 behaviour: if an EVENT\n            falling edge was queued for this safety flip, it is committed and\n            timestamped. Generic AUX state is handled explicitly and never by\n            this safety display flip.\n            """\n            next_physical_frame = global_frame + 1\n            flip_time = show_solid(GRAY)\n\n            if flip_time is not None:\n                for ttl_event in reversed(ttl_events):\n                    if ttl_event["scheduled_frame"] < next_physical_frame:\n                        break\n                    if ttl_event["scheduled_frame"] == next_physical_frame:\n                        ttl_event["flip_time_s"] = f"{float(flip_time):.9f}"\n\n            return flip_time\n\n        def abort_requested():\n            try:\n                return abort_map[0] == 1\n            except Exception:\n                return False\n\n        def check_abort():\n            if abort_requested():\n                raise KeyboardInterrupt\n            check_trigger_error()\n            if event.getKeys(keyList=["escape"]):\n                raise KeyboardInterrupt\n\n        def read_protocol_pause_request():\n            if not protocol_pause_path.exists():\n                return {\n                    "command_id": "",\n                    "pause_requested": False,\n                    "requested_at": "",\n                }\n\n            try:\n                return json.loads(\n                    protocol_pause_path.read_text(\n                        encoding="utf-8"\n                    )\n                )\n            except Exception:\n                return None\n\n        def wait_for_protocol_resume(\n            protocol_name,\n            protocol_version,\n            protocol_rep,\n            step_position,\n            total_steps,\n            next_step_name,\n            pause_events\n        ):\n            """\n            Pause is honored only before a protocol step begins.\n            The preceding step always completes normally.\n            """\n            global previous_flip, pacing_anchor_perf, pacing_anchor_frame\n\n            request = None\n            for _ in range(10):\n                request = read_protocol_pause_request()\n                if request is not None:\n                    break\n                core.wait(0.01)\n\n            if not request:\n                return\n\n            if (\n                str(request.get("command_id", ""))\n                != str(command_id)\n                or not bool(\n                    request.get(\n                        "pause_requested",\n                        False\n                    )\n                )\n            ):\n                return\n\n            # Unlogged gray flip: completes any pending one-frame EVENT LOW.\n            safe_gray_flip_and_stamp()\n            check_trigger_error()\n            previous_flip = None\n\n            paused_at_wall = (\n                datetime.now().isoformat(\n                    timespec="milliseconds"\n                )\n            )\n            paused_at_core = core.getTime()\n\n            write_json_atomic(\n                status_path,\n                {\n                    "state": "protocol_paused",\n                    "message":\n                        "PROTOCOLO EN PAUSA · fondo de adaptación",\n                    "hz": hz,\n                    "command_id": command_id,\n                    "protocol_name": protocol_name,\n                    "protocol_version": protocol_version,\n                    "protocol_rep": protocol_rep,\n                    "protocol_step": step_position,\n                    "protocol_total_steps": total_steps,\n                    "next_step_name": next_step_name,\n                    "pause_requested_at":\n                        request.get(\n                            "requested_at",\n                            ""\n                        ),\n                    "paused_at": paused_at_wall,\n                }\n            )\n\n            while True:\n                check_abort()\n                current = read_protocol_pause_request()\n\n                # A transient read error keeps the protocol paused.\n                if current is None:\n                    core.wait(0.05)\n                    continue\n\n                same_command = (\n                    str(\n                        current.get(\n                            "command_id",\n                            ""\n                        )\n                    )\n                    == str(command_id)\n                )\n                still_paused = (\n                    same_command\n                    and bool(\n                        current.get(\n                            "pause_requested",\n                            False\n                        )\n                    )\n                )\n\n                if not still_paused:\n                    break\n\n                core.wait(0.05)\n\n            resumed_at_wall = (\n                datetime.now().isoformat(\n                    timespec="milliseconds"\n                )\n            )\n            pause_duration_s = max(\n                0.0,\n                float(\n                    core.getTime()\n                    - paused_at_core\n                )\n            )\n\n            pause_events.append({\n                "pause_index":\n                    len(pause_events) + 1,\n                "requested_at":\n                    request.get(\n                        "requested_at",\n                        ""\n                    ),\n                "paused_at":\n                    paused_at_wall,\n                "resumed_at":\n                    resumed_at_wall,\n                "duration_s":\n                    pause_duration_s,\n                "before_protocol_rep":\n                    protocol_rep,\n                "before_step_position":\n                    step_position,\n                "before_step_name":\n                    next_step_name,\n                "safe_boundary":\n                    "between_protocol_steps",\n            })\n\n            # Refresh the adaptation background after the wait and start a new timing\n            # segment so manual pause time is never flagged as a frame drop.\n            show_solid(GRAY)\n            previous_flip = None\n            # Manual pauses are deliberately outside the experimental\n            # timeline. Restart absolute pacing at the next logged frame.\n            pacing_anchor_perf = None\n            pacing_anchor_frame = global_frame\n\n            write_json_atomic(\n                status_path,\n                {\n                    "state": "running",\n                    "message":\n                        (\n                            f"Reanudando protocolo {protocol_name} · "\n                            f"paso {step_position}/{total_steps}"\n                        ),\n                    "hz": hz,\n                    "command_id": command_id,\n                    "protocol_name": protocol_name,\n                    "protocol_version": protocol_version,\n                    "protocol_rep": protocol_rep,\n                    "protocol_step": step_position,\n                    "protocol_total_steps": total_steps,\n                    "resumed_from_pause": True,\n                }\n            )\n\n        def _wait_until_pacing_deadline(deadline):\n            """Wait efficiently until an absolute perf_counter deadline."""\n            start_wait = time.perf_counter()\n            while True:\n                remaining = float(deadline) - time.perf_counter()\n                if remaining <= 0:\n                    break\n                if remaining > 0.003:\n                    time.sleep(max(0.0, remaining - 0.0015))\n                # Final ~1.5 ms intentionally spins for timing precision.\n            return max(0.0, time.perf_counter() - start_wait)\n\n        def flip_and_log(stim_type, trial, condition, stage, frame_in_stage):\n            global previous_flip, global_frame, possible_drops\n            global pacing_anchor_perf, pacing_anchor_frame\n            global pacing_wait_total_s, pacing_late_frame_count\n            global pacing_max_lateness_s\n\n            check_trigger_error()\n\n            # Keep every logged frame on an absolute timeline. The first\n            # frame establishes the anchor; from then on frame N cannot be\n            # presented before anchor + N * expected_interval. If processing\n            # or the display makes us late, do not add another delay: this\n            # allows natural catch-up without ever running ahead overall.\n            next_global_frame = global_frame + 1\n            pacing_target_perf = None\n            if pacing_anchor_perf is not None:\n                pacing_target_perf = (\n                    pacing_anchor_perf\n                    + (next_global_frame - pacing_anchor_frame)\n                    * expected_interval\n                )\n                if time.perf_counter() < pacing_target_perf:\n                    pacing_wait_total_s += _wait_until_pacing_deadline(\n                        pacing_target_perf\n                    )\n\n            if ttl_backend is not None and not trigger_stopping:\n                win.callOnFlip(frame_sync_high)\n            try:\n                flip_time = win.flip()\n                flip_return_perf = time.perf_counter()\n            finally:\n                frame_sync_low()\n            # Raise I/O faults after all flip callbacks have been consumed.\n            check_trigger_error()\n\n            global_frame += 1\n\n            if pacing_anchor_perf is None:\n                pacing_anchor_perf = flip_return_perf\n                pacing_anchor_frame = global_frame\n            elif pacing_target_perf is not None:\n                lateness = max(0.0, flip_return_perf - pacing_target_perf)\n                # Ignore sub-0.25-ms call/clock noise in the diagnostic count.\n                if lateness > 0.00025:\n                    pacing_late_frame_count += 1\n                    pacing_max_lateness_s = max(\n                        pacing_max_lateness_s, lateness\n                    )\n\n            # Generic mode emits EVENT as the second pulse ~3 ms after the\n            # FRAME pulse of this same onset frame. Legacy MaxOne is untouched.\n            if ttl_mode == "generic_ftdi":\n                emit_generic_event_for_current_frame(flip_time)\n                check_trigger_error()\n\n            # callOnFlip callbacks have already run for this frame.\n            # Add the actual flip timestamp to every TTL event assigned\n            # to this exact global frame.\n            if flip_time is not None:\n                for ttl_event in reversed(ttl_events):\n                    if ttl_event["scheduled_frame"] < global_frame:\n                        break\n                    if ttl_event["scheduled_frame"] == global_frame:\n                        ttl_event["flip_time_s"] = (\n                            f"{float(flip_time):.9f}"\n                        )\n\n            interval = None\n            error_ms = None\n            dropped = False\n\n            if previous_flip is not None and flip_time is not None:\n                interval = float(flip_time - previous_flip)\n                error_ms = (\n                    interval - expected_interval\n                ) * 1000.0\n                dropped = (\n                    interval > expected_interval * 1.5\n                )\n                if dropped:\n                    possible_drops += 1\n\n            rows.append({\n                "stimulus_type": stim_type,\n                "trial": trial,\n                "condition": condition,\n                "stage": stage,\n                "frame_in_stage": frame_in_stage,\n                "global_frame": global_frame,\n                "flip_time_s":\n                    "" if flip_time is None\n                    else f"{float(flip_time):.9f}",\n                "interval_from_previous_flip_s":\n                    "" if interval is None\n                    else f"{interval:.9f}",\n                "expected_interval_s":\n                    f"{expected_interval:.9f}",\n                "interval_error_ms":\n                    "" if error_ms is None\n                    else f"{error_ms:.6f}",\n                "possible_dropped_frame":\n                    int(dropped),\n                "ttl_state": int(ttl_state),\n                "protocol_name":\n                    protocol_name_context,\n                "protocol_version":\n                    protocol_version_context,\n                "protocol_rep":\n                    protocol_rep_context,\n                "protocol_step_index":\n                    protocol_step_index_context,\n                "protocol_step_name":\n                    protocol_step_name_context,\n                "protocol_step_kind":\n                    protocol_step_kind_context,\n            })\n\n            if flip_time is not None:\n                previous_flip = float(flip_time)\n\n            # Event-pulse simulator: HIGH only on the onset frame.\n            # Queue the falling edge for the very next display flip.\n            finish_ttl_pulse_after_flip()\n\n        def solid_frames(\n            color, n_frames, stim_type, trial, condition, stage,\n            after_first_frame=None\n        ):\n            # Zero frames means no presentation and no hidden graphical\n            # state change.\n            if n_frames <= 0:\n                return\n\n            fullfield.fillColor = color\n            fullfield.lineColor = color\n            for frame_i in range(1, n_frames + 1):\n                check_abort()\n                fullfield.draw()\n                flip_and_log(\n                    stim_type, trial, condition,\n                    stage, frame_i\n                )\n                # v1.28: pause markers are emitted only after the first pause\n                # frame is physically visible. Existing calls remain unchanged.\n                if frame_i == 1 and after_first_frame is not None:\n                    after_first_frame()\n                    check_trigger_error()\n\n        def build_noise_resources(\n            noise_cfg\n        ):\n            """\n            Precompute one complete Noise stimulus in RAM.\n\n            Families:\n            - Dense White Noise: Binary or Ternary; new sequence per rep.\n            - Gaussian White Noise: continuous Gaussian; new sequence per rep.\n            - Frozen Noise: one seeded sequence replayed identically every rep.\n\n            Gaussian values are centered on Color medio. With contrast=1,\n            the requested low/high colors correspond approximately to ±3σ.\n            """\n            stim_total_f = seconds_to_frames(\n                noise_cfg[\n                    "dense_duration_s"\n                ],\n                hz\n            )\n            reps = int(\n                noise_cfg[\n                    "dense_repetitions"\n                ]\n            )\n            update_hz_req = float(\n                noise_cfg[\n                    "dense_update_hz"\n                ]\n            )\n            check_size_deg = float(\n                noise_cfg[\n                    "dense_check_size_deg"\n                ]\n            )\n            contrast = float(\n                noise_cfg[\n                    "dense_contrast"\n                ]\n            )\n            seed_base = int(\n                noise_cfg[\n                    "dense_seed"\n                ]\n            )\n\n            family_mode = str(\n                noise_cfg.get(\n                    "dense_family_mode",\n                    "Dense White Noise"\n                )\n            )\n            distribution = str(\n                noise_cfg.get(\n                    "dense_distribution",\n                    noise_cfg.get(\n                        "dense_mode",\n                        "Binary"\n                    )\n                )\n            ).lower()\n\n            if family_mode == "Gaussian White Noise":\n                distribution = "gaussian"\n                frozen = False\n            elif family_mode == "Dense White Noise":\n                frozen = False\n                if distribution not in (\n                    "binary",\n                    "ternary",\n                ):\n                    raise ValueError(\n                        "Dense White Noise usa Binary o Ternary."\n                    )\n            elif family_mode == "Frozen Noise":\n                frozen = True\n                if distribution not in (\n                    "binary",\n                    "ternary",\n                    "gaussian",\n                ):\n                    raise ValueError(\n                        "Frozen Noise: distribución no válida."\n                    )\n            else:\n                raise ValueError(\n                    f"Familia Noise desconocida: {family_mode}"\n                )\n\n            if reps < 1:\n                raise ValueError(\n                    "Noise: repeticiones debe ser >= 1."\n                )\n            if update_hz_req <= 0:\n                raise ValueError(\n                    "Noise: frecuencia de actualización debe ser > 0."\n                )\n            if check_size_deg <= 0:\n                raise ValueError(\n                    "Noise: tamaño de check debe ser > 0."\n                )\n            if not (\n                0.0\n                <= contrast\n                <= 1.0\n            ):\n                raise ValueError(\n                    "Noise: contraste debe estar entre 0 y 1."\n                )\n\n            frames_per_update = max(\n                1,\n                int(\n                    round(\n                        hz\n                        / update_hz_req\n                    )\n                )\n            )\n            actual_update_hz = (\n                hz\n                / frames_per_update\n            )\n            n_updates = max(\n                1,\n                int(\n                    math.ceil(\n                        stim_total_f\n                        / frames_per_update\n                    )\n                )\n            )\n\n            n_cols = max(\n                1,\n                int(\n                    math.ceil(\n                        width_deg\n                        / check_size_deg\n                    )\n                )\n            )\n            n_rows = max(\n                1,\n                int(\n                    math.ceil(\n                        height_deg\n                        / check_size_deg\n                    )\n                )\n            )\n\n            noise_size_deg = (\n                width_deg,\n                height_deg\n            )\n\n            color_low = np.asarray(\n                rgb255_to_psychopy(\n                    noise_cfg[\n                        "dense_color_low_rgb"\n                    ]\n                ),\n                dtype=np.float32\n            )\n            color_mid = np.asarray(\n                rgb255_to_psychopy(\n                    noise_cfg[\n                        "dense_color_mid_rgb"\n                    ]\n                ),\n                dtype=np.float32\n            )\n            color_high = np.asarray(\n                rgb255_to_psychopy(\n                    noise_cfg[\n                        "dense_color_high_rgb"\n                    ]\n                ),\n                dtype=np.float32\n            )\n\n            if distribution == "binary":\n                midpoint = (\n                    color_low\n                    + color_high\n                ) / 2.0\n                low_eff = (\n                    midpoint\n                    + contrast\n                    * (\n                        color_low\n                        - midpoint\n                    )\n                )\n                high_eff = (\n                    midpoint\n                    + contrast\n                    * (\n                        color_high\n                        - midpoint\n                    )\n                )\n                mid_eff = None\n\n            elif distribution == "ternary":\n                low_eff = (\n                    color_mid\n                    + contrast\n                    * (\n                        color_low\n                        - color_mid\n                    )\n                )\n                mid_eff = color_mid\n                high_eff = (\n                    color_mid\n                    + contrast\n                    * (\n                        color_high\n                        - color_mid\n                    )\n                )\n\n            else:\n                # Gaussian uses the full low/mid/high anchors. Contrast\n                # scales the normalized Gaussian values themselves.\n                low_eff = color_low\n                mid_eff = color_mid\n                high_eff = color_high\n\n            def make_sequence(seed):\n                check_abort()\n\n                rng = np.random.default_rng(\n                    seed\n                )\n\n                if distribution == "binary":\n                    states = rng.integers(\n                        0,\n                        2,\n                        size=(\n                            n_updates,\n                            n_rows,\n                            n_cols\n                        ),\n                        dtype=np.uint8\n                    )\n\n                elif distribution == "ternary":\n                    states = rng.integers(\n                        0,\n                        3,\n                        size=(\n                            n_updates,\n                            n_rows,\n                            n_cols\n                        ),\n                        dtype=np.uint8\n                    )\n\n                else:\n                    # Unit range represents the full low↔high excursion.\n                    # Base SD=1/3 means ±3σ approximately reaches ±1.\n                    states = rng.normal(\n                        loc=0.0,\n                        scale=(\n                            1.0\n                            / 3.0\n                        ),\n                        size=(\n                            n_updates,\n                            n_rows,\n                            n_cols\n                        )\n                    ).astype(\n                        np.float32\n                    )\n                    np.clip(\n                        states,\n                        -1.0,\n                        1.0,\n                        out=states\n                    )\n                    states *= contrast\n\n                rep_images = []\n\n                for upd in range(\n                    n_updates\n                ):\n                    check_abort()\n\n                    st = states[upd]\n                    img = np.empty(\n                        (\n                            n_rows,\n                            n_cols,\n                            3\n                        ),\n                        dtype=np.float32\n                    )\n\n                    if distribution == "binary":\n                        img[\n                            st == 0\n                        ] = low_eff\n                        img[\n                            st == 1\n                        ] = high_eff\n\n                    elif distribution == "ternary":\n                        img[\n                            st == 0\n                        ] = low_eff\n                        img[\n                            st == 1\n                        ] = mid_eff\n                        img[\n                            st == 2\n                        ] = high_eff\n\n                    else:\n                        img[:] = color_mid\n\n                        negative = (\n                            st < 0\n                        )\n                        positive = (\n                            st > 0\n                        )\n\n                        if np.any(\n                            negative\n                        ):\n                            weights = (\n                                -st[\n                                    negative\n                                ]\n                            )[:, None]\n                            img[\n                                negative\n                            ] = (\n                                color_mid\n                                + weights\n                                * (\n                                    color_low\n                                    - color_mid\n                                )\n                            )\n\n                        if np.any(\n                            positive\n                        ):\n                            weights = (\n                                st[\n                                    positive\n                                ]\n                            )[:, None]\n                            img[\n                                positive\n                            ] = (\n                                color_mid\n                                + weights\n                                * (\n                                    color_high\n                                    - color_mid\n                                )\n                            )\n\n                    rep_images.append(\n                        np.ascontiguousarray(\n                            img,\n                            dtype=np.float32\n                        )\n                    )\n\n                return (\n                    states,\n                    rep_images\n                )\n\n            state_sequences = []\n            image_sequences = []\n            used_seeds = []\n\n            if frozen:\n                base_states, base_images = (\n                    make_sequence(\n                        seed_base\n                    )\n                )\n\n                for rep in range(\n                    reps\n                ):\n                    state_sequences.append(\n                        base_states\n                    )\n                    image_sequences.append(\n                        base_images\n                    )\n                    used_seeds.append(\n                        seed_base\n                    )\n\n            else:\n                for rep in range(\n                    reps\n                ):\n                    seed = (\n                        seed_base\n                        + rep\n                    )\n                    states, rep_images = (\n                        make_sequence(\n                            seed\n                        )\n                    )\n                    state_sequences.append(\n                        states\n                    )\n                    image_sequences.append(\n                        rep_images\n                    )\n                    used_seeds.append(\n                        seed\n                    )\n\n            noise_stim = visual.ImageStim(\n                win=win,\n                image=image_sequences[\n                    0\n                ][0],\n                units="deg",\n                size=noise_size_deg,\n                pos=(0, 0),\n                colorSpace="rgb",\n                interpolate=False\n            )\n\n            return {\n                "family_mode":\n                    family_mode,\n                "distribution":\n                    distribution,\n                "frozen":\n                    frozen,\n                "frames_per_update":\n                    frames_per_update,\n                "actual_update_hz":\n                    actual_update_hz,\n                "n_updates":\n                    n_updates,\n                "n_cols":\n                    n_cols,\n                "n_rows":\n                    n_rows,\n                "noise_size_deg":\n                    noise_size_deg,\n                "state_sequences":\n                    state_sequences,\n                "image_sequences":\n                    image_sequences,\n                "used_seeds":\n                    used_seeds,\n                "noise_stim":\n                    noise_stim,\n            }\n\n        def save_noise_sequence_before_timing(\n            resources,\n            noise_cfg\n        ):\n            """\n            Save the exact Noise sequence before timed presentation.\n\n            Protocols call this during preload while neutral gray is visible.\n            Standalone Noise calls it before its first logged frame.\n            """\n            existing = resources.get(\n                "sequence_file"\n            )\n            if existing:\n                return str(existing)\n\n            family_mode = resources[\n                "family_mode"\n            ]\n            distribution_label = (\n                resources[\n                    "distribution"\n                ].capitalize()\n            )\n            frozen = bool(\n                resources[\n                    "frozen"\n                ]\n            )\n\n            stamp_seq = (\n                datetime.now().strftime(\n                    "%Y%m%d_%H%M%S_%f"\n                )\n            )\n            seq_path = run_dir / (\n                f"noise_sequence_"\n                f"{stamp_seq}.npz"\n            )\n\n            np.savez_compressed(\n                seq_path,\n                family_mode=np.asarray(\n                    [family_mode]\n                ),\n                distribution=np.asarray(\n                    [distribution_label]\n                ),\n                frozen=np.asarray(\n                    [frozen],\n                    dtype=np.bool_\n                ),\n                states=np.stack(\n                    resources[\n                        "state_sequences"\n                    ],\n                    axis=0\n                ),\n                used_seeds=np.asarray(\n                    resources[\n                        "used_seeds"\n                    ],\n                    dtype=np.int64\n                ),\n                check_size_deg=np.asarray(\n                    [\n                        float(\n                            noise_cfg[\n                                "dense_check_size_deg"\n                            ]\n                        )\n                    ],\n                    dtype=np.float32\n                ),\n                contrast=np.asarray(\n                    [\n                        float(\n                            noise_cfg[\n                                "dense_contrast"\n                            ]\n                        )\n                    ],\n                    dtype=np.float32\n                ),\n                requested_update_hz=np.asarray(\n                    [\n                        float(\n                            noise_cfg[\n                                "dense_update_hz"\n                            ]\n                        )\n                    ],\n                    dtype=np.float32\n                ),\n                actual_update_hz=np.asarray(\n                    [\n                        resources[\n                            "actual_update_hz"\n                        ]\n                    ],\n                    dtype=np.float32\n                ),\n                grid_rows=np.asarray(\n                    [\n                        resources[\n                            "n_rows"\n                        ]\n                    ],\n                    dtype=np.int32\n                ),\n                grid_cols=np.asarray(\n                    [\n                        resources[\n                            "n_cols"\n                        ]\n                    ],\n                    dtype=np.int32\n                ),\n            )\n\n            resources[\n                "sequence_file"\n            ] = str(\n                seq_path\n            )\n            resources[\n                "sequence_saved_before_timing"\n            ] = True\n\n            return str(\n                seq_path\n            )\n\n        def build_temporal_resources(\n            temporal_cfg\n        ):\n            """\n            Precompute frame/update schedules for the Temporal family.\n            This is used during Protocol Builder preload so no random\n            generation or schedule construction interrupts timed steps.\n            """\n            mode = str(\n                temporal_cfg.get(\n                    "temporal_mode",\n                    "Chirp + Intensity"\n                )\n            )\n\n            resources = {\n                "temporal_mode":\n                    mode\n            }\n\n            if mode == "Chirp + Intensity":\n                chirp_duration_f = seconds_to_frames(\n                    temporal_cfg[\n                        "chirp_frequency_duration_s"\n                    ],\n                    hz\n                )\n                f_min = float(\n                    temporal_cfg[\n                        "chirp_frequency_min_hz"\n                    ]\n                )\n                f_max = float(\n                    temporal_cfg[\n                        "chirp_frequency_max_hz"\n                    ]\n                )\n\n                if (\n                    f_min <= 0\n                    or f_max <= 0\n                ):\n                    raise ValueError(\n                        "Las frecuencias del chirp deben ser > 0."\n                    )\n                if f_max < f_min:\n                    raise ValueError(\n                        "La frecuencia máxima debe ser >= a la mínima."\n                    )\n                if f_max > hz / 2.0:\n                    raise ValueError(\n                        f"La frecuencia máxima ({f_max:.3f} Hz) supera "\n                        f"el límite representable de aproximadamente "\n                        f"{hz/2.0:.3f} Hz para este monitor."\n                    )\n\n                chirp_states = []\n                chirp_inst_freq = []\n\n                if chirp_duration_f > 0:\n                    if chirp_duration_f == 1:\n                        chirp_span_s = (\n                            1.0 / hz\n                        )\n                    else:\n                        chirp_span_s = (\n                            (\n                                chirp_duration_f\n                                - 1\n                            )\n                            / hz\n                        )\n\n                    slope = (\n                        (\n                            f_max\n                            - f_min\n                        )\n                        / chirp_span_s\n                        if chirp_span_s > 0\n                        else 0.0\n                    )\n\n                    for frame_idx in range(\n                        chirp_duration_f\n                    ):\n                        t = (\n                            frame_idx\n                            / hz\n                        )\n                        inst_f = (\n                            f_min\n                            + slope\n                            * t\n                        )\n                        phase_cycles = (\n                            f_min\n                            * t\n                            + 0.5\n                            * slope\n                            * t\n                            * t\n                        )\n                        is_on = (\n                            (\n                                phase_cycles\n                                % 1.0\n                            )\n                            < 0.5\n                        )\n                        chirp_states.append(\n                            is_on\n                        )\n                        chirp_inst_freq.append(\n                            inst_f\n                        )\n\n                intensity_duration_f = seconds_to_frames(\n                    temporal_cfg[\n                        "chirp_intensity_duration_s"\n                    ],\n                    hz\n                )\n                intensity_freq = float(\n                    temporal_cfg[\n                        "chirp_intensity_frequency_hz"\n                    ]\n                )\n                intensity_start_pct = float(\n                    temporal_cfg[\n                        "chirp_intensity_start_pct"\n                    ]\n                )\n\n                if intensity_freq <= 0:\n                    raise ValueError(\n                        "La frecuencia de la rampa de intensidad debe ser > 0."\n                    )\n                if intensity_freq > hz / 2.0:\n                    raise ValueError(\n                        f"La frecuencia de intensidad "\n                        f"({intensity_freq:.3f} Hz) supera el límite "\n                        f"representable de aproximadamente "\n                        f"{hz/2.0:.3f} Hz para este monitor."\n                    )\n                if not (\n                    0.0\n                    <= intensity_start_pct\n                    <= 100.0\n                ):\n                    raise ValueError(\n                        "La intensidad ON inicial debe estar entre 0 y 100 %."\n                    )\n\n                intensity_states = []\n                intensity_levels_pct = []\n\n                if intensity_duration_f > 0:\n                    # Derive the number of cycles from the cycle indices\n                    # that are actually reachable by the rendered frames.\n                    # This avoids a theoretical extra cycle caused by\n                    # ceil(duration * frequency) after seconds→frames\n                    # rounding, and guarantees that the final ON cycle\n                    # reaches exactly 100 %.\n                    last_t = (\n                        (\n                            intensity_duration_f\n                            - 1\n                        )\n                        / hz\n                    )\n                    last_phase_cycles = (\n                        last_t\n                        * intensity_freq\n                    )\n                    max_cycle_index = int(\n                        math.floor(\n                            last_phase_cycles\n                        )\n                    )\n                    n_cycles = max(\n                        1,\n                        max_cycle_index\n                        + 1\n                    )\n\n                    for frame_idx in range(\n                        intensity_duration_f\n                    ):\n                        t = (\n                            frame_idx\n                            / hz\n                        )\n                        phase_cycles = (\n                            t\n                            * intensity_freq\n                        )\n                        cycle_index = min(\n                            n_cycles - 1,\n                            int(\n                                math.floor(\n                                    phase_cycles\n                                )\n                            )\n                        )\n                        is_on = (\n                            (\n                                phase_cycles\n                                % 1.0\n                            )\n                            < 0.5\n                        )\n\n                        if n_cycles <= 1:\n                            pct = 100.0\n                        else:\n                            pct = (\n                                intensity_start_pct\n                                + (\n                                    100.0\n                                    - intensity_start_pct\n                                )\n                                * (\n                                    cycle_index\n                                    / (\n                                        n_cycles\n                                        - 1\n                                    )\n                                )\n                            )\n\n                        intensity_states.append(\n                            is_on\n                        )\n                        intensity_levels_pct.append(\n                            pct\n                        )\n\n                resources.update({\n                    "chirp_states":\n                        chirp_states,\n                    "chirp_inst_freq":\n                        chirp_inst_freq,\n                    "intensity_states":\n                        intensity_states,\n                    "intensity_levels_pct":\n                        intensity_levels_pct,\n                })\n\n                return resources\n\n            duration_f = seconds_to_frames(\n                temporal_cfg[\n                    "temporal_duration_s"\n                ],\n                hz\n            )\n            if duration_f <= 0:\n                raise ValueError(\n                    f"{mode}: duración debe ser > 0."\n                )\n\n            if mode in (\n                "Sinusoidal Flicker",\n                "Contrast Series",\n            ):\n                frequency = float(\n                    temporal_cfg[\n                        "temporal_frequency_hz"\n                    ]\n                )\n                if frequency <= 0:\n                    raise ValueError(\n                        f"{mode}: frecuencia debe ser > 0."\n                    )\n                if frequency >= hz / 2.0:\n                    raise ValueError(\n                        f"{mode}: la frecuencia ({frequency:.3f} Hz) "\n                        f"debe ser menor que Nyquist "\n                        f"({hz/2.0:.3f} Hz) para este monitor."\n                    )\n\n                t = (\n                    np.arange(\n                        duration_f,\n                        dtype=np.float64\n                    )\n                    / hz\n                )\n                resources[\n                    "sine_values"\n                ] = np.sin(\n                    2.0\n                    * math.pi\n                    * frequency\n                    * t\n                ).astype(\n                    np.float32\n                )\n\n            if mode == "Sinusoidal Flicker":\n                contrast_pct = float(\n                    temporal_cfg[\n                        "temporal_contrast_pct"\n                    ]\n                )\n                if not (\n                    0.0\n                    <= contrast_pct\n                    <= 100.0\n                ):\n                    raise ValueError(\n                        "Sinusoidal Flicker: contraste debe estar entre 0 y 100 %."\n                    )\n\n                resources[\n                    "contrast_fraction"\n                ] = (\n                    contrast_pct\n                    / 100.0\n                )\n                return resources\n\n            if mode == "Gaussian Flicker":\n                update_hz_req = float(\n                    temporal_cfg[\n                        "temporal_update_hz"\n                    ]\n                )\n                contrast_pct = float(\n                    temporal_cfg[\n                        "temporal_contrast_pct"\n                    ]\n                )\n                reps = int(\n                    temporal_cfg[\n                        "chirp_repetitions"\n                    ]\n                )\n                seed_base = int(\n                    temporal_cfg[\n                        "temporal_seed"\n                    ]\n                )\n\n                if update_hz_req <= 0:\n                    raise ValueError(\n                        "Gaussian Flicker: frecuencia de actualización debe ser > 0."\n                    )\n                if not (\n                    0.0\n                    <= contrast_pct\n                    <= 100.0\n                ):\n                    raise ValueError(\n                        "Gaussian Flicker: contraste debe estar entre 0 y 100 %."\n                    )\n                if reps < 1:\n                    raise ValueError(\n                        "Temporal: repeticiones debe ser >= 1."\n                    )\n\n                frames_per_update = max(\n                    1,\n                    int(\n                        round(\n                            hz\n                            / update_hz_req\n                        )\n                    )\n                )\n                actual_update_hz = (\n                    hz\n                    / frames_per_update\n                )\n                n_updates = max(\n                    1,\n                    int(\n                        math.ceil(\n                            duration_f\n                            / frames_per_update\n                        )\n                    )\n                )\n\n                sequences = []\n                used_seeds = []\n\n                for rep_idx in range(\n                    reps\n                ):\n                    seed = (\n                        seed_base\n                        + rep_idx\n                    )\n                    rng = np.random.default_rng(\n                        seed\n                    )\n                    vals = rng.normal(\n                        loc=0.0,\n                        scale=(\n                            1.0\n                            / 3.0\n                        ),\n                        size=n_updates\n                    ).astype(\n                        np.float32\n                    )\n                    vals = np.clip(\n                        vals,\n                        -1.0,\n                        1.0\n                    )\n                    sequences.append(\n                        vals\n                    )\n                    used_seeds.append(\n                        seed\n                    )\n\n                resources.update({\n                    "frames_per_update":\n                        frames_per_update,\n                    "actual_update_hz":\n                        actual_update_hz,\n                    "n_updates":\n                        n_updates,\n                    "gaussian_sequences":\n                        sequences,\n                    "used_seeds":\n                        used_seeds,\n                    "contrast_fraction":\n                        (\n                            contrast_pct\n                            / 100.0\n                        ),\n                })\n                return resources\n\n            if mode == "Contrast Series":\n                levels = parse_float_list(\n                    temporal_cfg[\n                        "temporal_contrast_levels_pct"\n                    ]\n                )\n                if not levels:\n                    raise ValueError(\n                        "Contrast Series: no hay niveles de contraste."\n                    )\n                if any(\n                    (\n                        level < 0.0\n                        or level > 100.0\n                    )\n                    for level in levels\n                ):\n                    raise ValueError(\n                        "Contrast Series: todos los contrastes deben estar entre 0 y 100 %."\n                    )\n\n                resources[\n                    "contrast_levels_pct"\n                ] = levels\n                return resources\n\n            raise ValueError(\n                f"Modo Temporal desconocido: {mode}"\n            )\n\n        try:\n            # Open the physical trigger backend only when a run starts.\n            # Simulation/disabled modes are no-ops here.\n            open_trigger_backend()\n\n            def prepare_protocol_step(step):\n                """\n                Build heavy PsychoPy/OpenGL resources before protocol timing\n                starts. Any warm-up draw is covered by the adaptation background before\n                the flip, so the experimental display never shows the\n                stimulus during preparation.\n                """\n                if step.get("kind") != "stimulus":\n                    return {\n                        "stimulus_type": "pause"\n                    }\n\n                step_cfg = dict(\n                    step.get("config", {})\n                )\n                stype = str(\n                    step_cfg.get(\n                        "stimulus_type",\n                        step.get(\n                            "stimulus_type", ""\n                        )\n                    )\n                )\n\n                prepared = {\n                    "stimulus_type": stype\n                }\n\n                def warm_draw(draw_callables):\n                    check_abort()\n                    for draw_callable in draw_callables:\n                        draw_callable()\n\n                    # Cover everything in the back buffer with the adaptation background\n                    # before flipping.\n                    fullfield.fillColor = GRAY\n                    fullfield.lineColor = GRAY\n                    fullfield.draw()\n                    win.flip()\n\n                if stype == "motion":\n                    mode = str(\n                        step_cfg[\n                            "motion_mode"\n                        ]\n                    )\n\n                    stim_rgb = rgb255_to_psychopy(\n                        step_cfg[\n                            "motion_stimulus_rgb"\n                        ]\n                    )\n\n                    prepared[\n                        "motion_mode"\n                    ] = mode\n\n                    if mode == "Moving Bar":\n                        bar = visual.Rect(\n                            win=win,\n                            width=float(\n                                step_cfg[\n                                    "motion_bar_length_deg"\n                                ]\n                            ),\n                            height=float(\n                                step_cfg[\n                                    "motion_bar_width_deg"\n                                ]\n                            ),\n                            units="deg",\n                            fillColor=stim_rgb,\n                            lineColor=stim_rgb,\n                            colorSpace="rgb",\n                            pos=(0, 0)\n                        )\n                        prepared[\n                            "motion_stim"\n                        ] = bar\n                        warm_draw([\n                            bar.draw\n                        ])\n\n                    else:\n                        if mode == "Moving Spot":\n                            diameter = float(\n                                step_cfg[\n                                    "motion_spot_diameter_deg"\n                                ]\n                            )\n                        elif mode in (\n                            "Expanding Annulus",\n                            "Receding Annulus",\n                        ):\n                            diameter = float(\n                                step_cfg.get(\n                                    "motion_annulus_large_diameter_deg",\n                                    40.0\n                                )\n                            )\n                        else:\n                            diameter = float(\n                                step_cfg[\n                                    "motion_large_diameter_deg"\n                                ]\n                            )\n\n                        spot = visual.Circle(\n                            win=win,\n                            radius=diameter / 2.0,\n                            units="deg",\n                            pos=(0, 0),\n                            fillColor=stim_rgb,\n                            lineColor=stim_rgb,\n                            colorSpace="rgb"\n                        )\n                        prepared[\n                            "motion_stim"\n                        ] = spot\n\n                        if mode in (\n                            "Expanding Annulus",\n                            "Receding Annulus",\n                        ):\n                            thickness = float(\n                                step_cfg.get(\n                                    "motion_annulus_thickness_deg",\n                                    2.0\n                                )\n                            )\n                            bg_rgb = rgb255_to_psychopy(\n                                step_cfg[\n                                    "motion_background_rgb"\n                                ]\n                            )\n                            inner_diameter = (\n                                diameter\n                                - 2.0\n                                * thickness\n                            )\n\n                            if inner_diameter <= 0:\n                                raise ValueError(\n                                    "Annulus: grosor radial incompatible "\n                                    "con el diámetro exterior."\n                                )\n\n                            inner = visual.Circle(\n                                win=win,\n                                radius=inner_diameter / 2.0,\n                                units="deg",\n                                pos=(0, 0),\n                                fillColor=bg_rgb,\n                                lineColor=bg_rgb,\n                                colorSpace="rgb"\n                            )\n                            prepared[\n                                "motion_inner_stim"\n                            ] = inner\n\n                            warm_draw([\n                                spot.draw,\n                                inner.draw,\n                            ])\n                        else:\n                            warm_draw([\n                                spot.draw\n                            ])\n\n                elif stype == "grating":\n                    mode = str(\n                        step_cfg.get(\n                            "grating_mode",\n                            "Drifting"\n                        )\n                    )\n                    if mode == "Gabor":\n                        mode = "Static Gabor"\n\n                    sf = float(\n                        step_cfg[\n                            "grating_sf_cpd"\n                        ]\n                    )\n                    adjustable_contrast = float(\n                        step_cfg[\n                            "grating_contrast"\n                        ]\n                    )\n                    absolute = bool(\n                        step_cfg[\n                            "grating_absolute_contrast"\n                        ]\n                    )\n                    waveform = str(\n                        step_cfg[\n                            "grating_waveform"\n                        ]\n                    )\n                    asymmetric_polarity = str(\n                        step_cfg.get(\n                            "grating_asymmetric_polarity",\n                            "A_TO_B"\n                        )\n                    )\n                    contrast_used = (\n                        1.0\n                        if absolute\n                        else adjustable_contrast\n                    )\n\n                    if mode == "Asymmetric Drift":\n                        texture = asymmetric_grating_texture(\n                            step_cfg[\n                                "grating_color_a_rgb"\n                            ],\n                            step_cfg[\n                                "grating_color_b_rgb"\n                            ],\n                            contrast_used,\n                            asymmetric_polarity\n                        )\n                    else:\n                        texture = colored_grating_texture(\n                            step_cfg[\n                                "grating_color_a_rgb"\n                            ],\n                            step_cfg[\n                                "grating_color_b_rgb"\n                            ],\n                            waveform,\n                            contrast_used\n                        )\n\n                    if mode in (\n                        "Static Gabor",\n                        "Drifting Gabor",\n                    ):\n                        grat_size = float(\n                            step_cfg.get(\n                                "grating_gabor_size_deg",\n                                30.0\n                            )\n                        )\n                        sigma = float(\n                            step_cfg.get(\n                                "grating_gabor_sigma_deg",\n                                6.0\n                            )\n                        )\n                        mask = gaussian_mask_deg(\n                            grat_size,\n                            sigma\n                        )\n                        pos = (\n                            float(\n                                step_cfg.get(\n                                    "grating_gabor_x_deg",\n                                    0.0\n                                )\n                            ),\n                            float(\n                                step_cfg.get(\n                                    "grating_gabor_y_deg",\n                                    0.0\n                                )\n                            )\n                        )\n                    else:\n                        grat_size = (\n                            2.2\n                            * math.hypot(\n                                width_deg,\n                                height_deg\n                            )\n                        )\n                        mask = None\n                        pos = (0, 0)\n\n                    grating = visual.GratingStim(\n                        win=win,\n                        tex=texture,\n                        mask=mask,\n                        units="deg",\n                        size=(\n                            grat_size,\n                            grat_size\n                        ),\n                        sf=sf,\n                        contrast=1.0,\n                        pos=pos,\n                        color=(1, 1, 1),\n                        colorSpace="rgb",\n                        interpolate=True\n                    )\n\n                    prepared[\n                        "grating_mode"\n                    ] = mode\n                    prepared[\n                        "texture"\n                    ] = texture\n                    prepared[\n                        "grating"\n                    ] = grating\n\n                    if mode in (\n                        "Static Gabor",\n                        "Drifting Gabor",\n                    ):\n                        prepared[\n                            "gabor_mask"\n                        ] = mask\n\n                    warm_draw([\n                        grating.draw\n                    ])\n\n                elif stype == "dense_noise":\n                    prepared.update(\n                        build_noise_resources(\n                            step_cfg\n                        )\n                    )\n\n                    warm_draw([\n                        prepared[\n                            "noise_stim"\n                        ].draw\n                    ])\n\n                    save_noise_sequence_before_timing(\n                        prepared,\n                        step_cfg\n                    )\n\n                elif stype == "rf_mapping":\n                    mode = str(\n                        step_cfg[\n                            "rf_mode"\n                        ]\n                    ).strip().lower()\n\n                    x_deg = float(\n                        step_cfg["rf_x_deg"]\n                    )\n                    y_deg = float(\n                        step_cfg["rf_y_deg"]\n                    )\n                    stim_rgb = rgb255_to_psychopy(\n                        step_cfg[\n                            "rf_stimulus_rgb"\n                        ]\n                    )\n                    bg_rgb = rgb255_to_psychopy(\n                        step_cfg[\n                            "rf_background_rgb"\n                        ]\n                    )\n\n                    prepared["rf_mode"] = (\n                        mode\n                    )\n\n                    if mode == "spot":\n                        diameter = float(\n                            step_cfg[\n                                "rf_spot_diameter_deg"\n                            ]\n                        )\n\n                        spot = visual.Circle(\n                            win=win,\n                            radius=(\n                                diameter\n                                / 2.0\n                            ),\n                            units="deg",\n                            pos=(\n                                x_deg,\n                                y_deg\n                            ),\n                            fillColor=stim_rgb,\n                            lineColor=stim_rgb,\n                            colorSpace="rgb"\n                        )\n                        prepared[\n                            "spot"\n                        ] = spot\n                        warm_draw([\n                            spot.draw\n                        ])\n\n                    elif mode == "annulus":\n                        inner = float(\n                            step_cfg[\n                                "rf_annulus_inner_deg"\n                            ]\n                        )\n                        outer = float(\n                            step_cfg[\n                                "rf_annulus_outer_deg"\n                            ]\n                        )\n\n                        outer_circle = (\n                            visual.Circle(\n                                win=win,\n                                radius=(\n                                    outer\n                                    / 2.0\n                                ),\n                                units="deg",\n                                pos=(\n                                    x_deg,\n                                    y_deg\n                                ),\n                                fillColor=stim_rgb,\n                                lineColor=stim_rgb,\n                                colorSpace="rgb"\n                            )\n                        )\n                        inner_circle = (\n                            visual.Circle(\n                                win=win,\n                                radius=(\n                                    inner\n                                    / 2.0\n                                ),\n                                units="deg",\n                                pos=(\n                                    x_deg,\n                                    y_deg\n                                ),\n                                fillColor=bg_rgb,\n                                lineColor=bg_rgb,\n                                colorSpace="rgb"\n                            )\n                        )\n\n                        prepared[\n                            "outer_circle"\n                        ] = outer_circle\n                        prepared[\n                            "inner_circle"\n                        ] = inner_circle\n                        warm_draw([\n                            outer_circle.draw,\n                            inner_circle.draw\n                        ])\n\n                    elif mode == "annulus size series":\n                        sizes = parse_float_list(\n                            step_cfg[\n                                "rf_annulus_size_series_deg"\n                            ]\n                        )\n                        thickness = float(\n                            step_cfg[\n                                "rf_annulus_series_thickness_deg"\n                            ]\n                        )\n\n                        if not sizes:\n                            raise ValueError(\n                                "RF Annulus Size Series: no hay diámetros."\n                            )\n\n                        if (\n                            thickness <= 0\n                            or any(\n                                2.0 * thickness >= diameter\n                                for diameter in sizes\n                            )\n                        ):\n                            raise ValueError(\n                                "RF Annulus Size Series: grosor incompatible "\n                                "con los diámetros."\n                            )\n\n                        # Build and prewarm ONE independent outer/inner\n                        # Circle pair for EVERY requested diameter. This\n                        # avoids changing Circle.radius for the first time\n                        # during a timed condition.\n                        annulus_size_pairs = []\n\n                        for diameter in sizes:\n                            inner_diameter = (\n                                diameter\n                                - 2.0 * thickness\n                            )\n\n                            outer_circle = visual.Circle(\n                                win=win,\n                                radius=diameter / 2.0,\n                                units="deg",\n                                pos=(\n                                    x_deg,\n                                    y_deg\n                                ),\n                                fillColor=stim_rgb,\n                                lineColor=stim_rgb,\n                                colorSpace="rgb"\n                            )\n                            inner_circle = visual.Circle(\n                                win=win,\n                                radius=inner_diameter / 2.0,\n                                units="deg",\n                                pos=(\n                                    x_deg,\n                                    y_deg\n                                ),\n                                fillColor=bg_rgb,\n                                lineColor=bg_rgb,\n                                colorSpace="rgb"\n                            )\n\n                            annulus_size_pairs.append(\n                                (\n                                    float(diameter),\n                                    outer_circle,\n                                    inner_circle,\n                                )\n                            )\n\n                            warm_draw([\n                                outer_circle.draw,\n                                inner_circle.draw\n                            ])\n\n                        prepared[\n                            "annulus_size_series"\n                        ] = sizes\n                        prepared[\n                            "annulus_size_thickness"\n                        ] = thickness\n                        prepared[\n                            "annulus_size_pairs"\n                        ] = annulus_size_pairs\n\n                    elif mode == "size series":\n                        sizes = parse_float_list(\n                            step_cfg[\n                                "rf_size_series_deg"\n                            ]\n                        )\n                        size_spots = []\n\n                        for diameter in sizes:\n                            spot = visual.Circle(\n                                win=win,\n                                radius=(\n                                    diameter\n                                    / 2.0\n                                ),\n                                units="deg",\n                                pos=(\n                                    x_deg,\n                                    y_deg\n                                ),\n                                fillColor=stim_rgb,\n                                lineColor=stim_rgb,\n                                colorSpace="rgb"\n                            )\n                            size_spots.append(\n                                spot\n                            )\n\n                        prepared[\n                            "size_spots"\n                        ] = size_spots\n\n                        if size_spots:\n                            warm_draw([\n                                spot.draw\n                                for spot\n                                in size_spots\n                            ])\n\n                    elif mode == "sparse noise":\n                        square_size = float(\n                            step_cfg[\n                                "rf_sparse_square_deg"\n                            ]\n                        )\n                        grid_step = float(\n                            step_cfg[\n                                "rf_sparse_grid_step_deg"\n                            ]\n                        )\n\n                        half = (\n                            square_size\n                            / 2.0\n                        )\n                        x_min = (\n                            -width_deg\n                            / 2.0\n                            + half\n                        )\n                        x_max = (\n                            width_deg\n                            / 2.0\n                            - half\n                        )\n                        y_min = (\n                            -height_deg\n                            / 2.0\n                            + half\n                        )\n                        y_max = (\n                            height_deg\n                            / 2.0\n                            - half\n                        )\n\n                        xs = np.arange(\n                            x_min,\n                            x_max\n                            + grid_step\n                            * 0.25,\n                            grid_step\n                        )\n                        ys = np.arange(\n                            y_min,\n                            y_max\n                            + grid_step\n                            * 0.25,\n                            grid_step\n                        )\n\n                        if len(xs) == 0:\n                            xs = np.asarray(\n                                [0.0]\n                            )\n                        if len(ys) == 0:\n                            ys = np.asarray(\n                                [0.0]\n                            )\n\n                        positions = [\n                            (\n                                float(x),\n                                float(y)\n                            )\n                            for y in ys\n                            for x in xs\n                        ]\n\n                        sparse = visual.Rect(\n                            win=win,\n                            width=square_size,\n                            height=square_size,\n                            units="deg",\n                            fillColor=WHITE,\n                            lineColor=WHITE,\n                            colorSpace="rgb",\n                            pos=(0, 0)\n                        )\n\n                        prepared[\n                            "positions"\n                        ] = positions\n                        prepared[\n                            "sparse"\n                        ] = sparse\n                        warm_draw([\n                            sparse.draw\n                        ])\n\n                elif stype == "chirp_intensity":\n                    prepared.update(\n                        build_temporal_resources(\n                            step_cfg\n                        )\n                    )\n\n                # ON/OFF uses the already-existing full-field object.\n                return prepared\n\n            def run_current_stimulus():\n                global details\n                # ==========================================================\n                # ON / OFF\n                # ==========================================================\n                if stimulus_type == "on_off":\n                    baseline_f = seconds_to_frames(\n                        cfg["baseline_s"], hz\n                    )\n                    on_f = seconds_to_frames(\n                        cfg["on_s"], hz\n                    )\n                    gray_f = seconds_to_frames(\n                        cfg["gray_interval_s"], hz\n                    )\n                    off_f = seconds_to_frames(\n                        cfg["off_s"], hz\n                    )\n                    final_f = seconds_to_frames(\n                        cfg["final_gray_s"], hz\n                    )\n                    reps = int(\n                        cfg["repetitions"]\n                    )\n\n                    on_color = rgb255_to_psychopy(\n                        cfg["on_color_rgb"]\n                    )\n                    off_color = rgb255_to_psychopy(\n                        cfg["off_color_rgb"]\n                    )\n\n                    details = {\n                        "baseline_frames":\n                            baseline_f,\n                        "on_frames":\n                            on_f,\n                        "gray_interval_frames":\n                            gray_f,\n                        "off_frames":\n                            off_f,\n                        "final_gray_frames":\n                            final_f,\n                        "on_color_rgb_255":\n                            cfg["on_color_rgb"],\n                        "off_color_rgb_255":\n                            cfg["off_color_rgb"],\n                    }\n\n                    for rep in range(\n                        1, reps + 1\n                    ):\n                        solid_frames(\n                            GRAY,\n                            baseline_f,\n                            "on_off",\n                            rep,\n                            "ON/OFF",\n                            "baseline_gray"\n                        )\n\n                        if on_f > 0:\n                            queue_ttl_event(\n                                "ON_START",\n                                rep,\n                                "ON"\n                            )\n\n                        solid_frames(\n                            on_color,\n                            on_f,\n                            "on_off",\n                            rep,\n                            "ON",\n                            "on"\n                        )\n\n                        solid_frames(\n                            GRAY,\n                            gray_f,\n                            "on_off",\n                            rep,\n                            "ON/OFF",\n                            "gray_interval"\n                        )\n\n                        if off_f > 0:\n                            queue_ttl_event(\n                                "OFF_START",\n                                rep,\n                                "OFF"\n                            )\n\n                        solid_frames(\n                            off_color,\n                            off_f,\n                            "on_off",\n                            rep,\n                            "OFF",\n                            "off"\n                        )\n\n                        solid_frames(\n                            GRAY,\n                            final_f,\n                            "on_off",\n                            rep,\n                            "ON/OFF",\n                            "final_gray"\n                        )\n\n                        write_json_atomic(\n                            status_path,\n                            {\n                                "state":\n                                    "running",\n                                "message":\n                                    f"ON/OFF · repetición "\n                                    f"{rep}/{reps}",\n                                "hz": hz,\n                                "command_id":\n                                    command_id,\n                                "details":\n                                    details\n                            }\n                        )\n\n                elif stimulus_type == "motion":\n                    mode = str(\n                        cfg["motion_mode"]\n                    )\n                    baseline_f = seconds_to_frames(\n                        cfg["motion_baseline_s"],\n                        hz\n                    )\n                    stim_f = seconds_to_frames(\n                        cfg["motion_duration_s"],\n                        hz\n                    )\n                    isi_f = seconds_to_frames(\n                        cfg["motion_isi_s"],\n                        hz\n                    )\n                    reps = int(\n                        cfg["motion_repetitions"]\n                    )\n\n                    background_rgb = rgb255_to_psychopy(\n                        cfg[\n                            "motion_background_rgb"\n                        ]\n                    )\n                    stimulus_rgb = rgb255_to_psychopy(\n                        cfg[\n                            "motion_stimulus_rgb"\n                        ]\n                    )\n\n                    if stim_f <= 0:\n                        raise ValueError(\n                            "Motion: la duración debe producir "\n                            "al menos 1 frame."\n                        )\n                    if reps < 1:\n                        raise ValueError(\n                            "Motion: repeticiones debe ser >= 1."\n                        )\n\n                    if (\n                        prepared_step is not None\n                        and prepared_step.get(\n                            "stimulus_type"\n                        ) == "motion"\n                        and "motion_stim"\n                        in prepared_step\n                    ):\n                        motion_stim = prepared_step[\n                            "motion_stim"\n                        ]\n                        motion_inner_stim = (\n                            prepared_step.get(\n                                "motion_inner_stim"\n                            )\n                        )\n                    else:\n                        motion_stim = None\n                        motion_inner_stim = None\n\n                    details = {\n                        "mode": mode,\n                        "baseline_frames":\n                            baseline_f,\n                        "stimulus_frames":\n                            stim_f,\n                        "isi_frames":\n                            isi_f,\n                        "repetitions":\n                            reps,\n                        "background_rgb_255":\n                            cfg[\n                                "motion_background_rgb"\n                            ],\n                        "stimulus_rgb_255":\n                            cfg[\n                                "motion_stimulus_rgb"\n                            ],\n                    }\n\n                    fullfield.fillColor = (\n                        background_rgb\n                    )\n                    fullfield.lineColor = (\n                        background_rgb\n                    )\n\n                    trial = 0\n\n                    if mode in (\n                        "Moving Bar",\n                        "Moving Spot",\n                    ):\n                        directions = parse_float_list(\n                            cfg[\n                                "motion_directions"\n                            ]\n                        )\n                        if not directions:\n                            raise ValueError(\n                                "Motion: no hay direcciones."\n                            )\n\n                        details[\n                            "directions"\n                        ] = directions\n\n                        if mode == "Moving Bar":\n                            bar_width = float(\n                                cfg[\n                                    "motion_bar_width_deg"\n                                ]\n                            )\n                            bar_length = float(\n                                cfg[\n                                    "motion_bar_length_deg"\n                                ]\n                            )\n\n                            if (\n                                bar_width <= 0\n                                or bar_length <= 0\n                            ):\n                                raise ValueError(\n                                    "Motion: dimensiones de barra "\n                                    "deben ser > 0."\n                                )\n\n                            details[\n                                "bar_width_deg"\n                            ] = bar_width\n                            details[\n                                "bar_length_deg"\n                            ] = bar_length\n\n                            if motion_stim is None:\n                                motion_stim = visual.Rect(\n                                    win=win,\n                                    width=bar_length,\n                                    height=bar_width,\n                                    units="deg",\n                                    fillColor=stimulus_rgb,\n                                    lineColor=stimulus_rgb,\n                                    colorSpace="rgb",\n                                    pos=(0, 0)\n                                )\n\n                        else:\n                            spot_diameter = float(\n                                cfg[\n                                    "motion_spot_diameter_deg"\n                                ]\n                            )\n                            if spot_diameter <= 0:\n                                raise ValueError(\n                                    "Motion: diámetro del spot "\n                                    "debe ser > 0."\n                                )\n\n                            details[\n                                "spot_diameter_deg"\n                            ] = spot_diameter\n\n                            if motion_stim is None:\n                                motion_stim = visual.Circle(\n                                    win=win,\n                                    radius=(\n                                        spot_diameter\n                                        / 2.0\n                                    ),\n                                    units="deg",\n                                    pos=(0, 0),\n                                    fillColor=stimulus_rgb,\n                                    lineColor=stimulus_rgb,\n                                    colorSpace="rgb"\n                                )\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            for direction in directions:\n                                trial += 1\n\n                                condition = (\n                                    f"{mode} "\n                                    f"{direction:g}°"\n                                )\n\n                                solid_frames(\n                                    GRAY,\n                                    baseline_f,\n                                    "motion",\n                                    trial,\n                                    condition,\n                                    "baseline_gray"\n                                )\n\n                                theta = math.radians(\n                                    direction\n                                )\n                                dx = math.cos(theta)\n                                dy = math.sin(theta)\n\n                                screen_half_extent = (\n                                    abs(dx)\n                                    * width_deg\n                                    / 2.0\n                                    + abs(dy)\n                                    * height_deg\n                                    / 2.0\n                                )\n\n                                if mode == "Moving Bar":\n                                    radius_along_motion = (\n                                        bar_width\n                                        / 2.0\n                                    )\n                                    motion_stim.ori = (\n                                        90.0\n                                        - direction\n                                    )\n                                    ttl_name = (\n                                        "MOTION_BAR_START"\n                                    )\n                                    stage_name = (\n                                        "moving_bar"\n                                    )\n                                else:\n                                    radius_along_motion = (\n                                        spot_diameter\n                                        / 2.0\n                                    )\n                                    ttl_name = (\n                                        "MOTION_SPOT_START"\n                                    )\n                                    stage_name = (\n                                        "moving_spot"\n                                    )\n\n                                travel_half = (\n                                    screen_half_extent\n                                    + radius_along_motion\n                                )\n\n                                queue_ttl_event(\n                                    ttl_name,\n                                    trial,\n                                    condition\n                                )\n\n                                for f in range(\n                                    1, stim_f + 1\n                                ):\n                                    check_abort()\n\n                                    u = (\n                                        0.0\n                                        if stim_f <= 1\n                                        else (\n                                            (f - 1)\n                                            / (stim_f - 1)\n                                        )\n                                    )\n                                    scalar = (\n                                        -travel_half\n                                        + 2.0\n                                        * travel_half\n                                        * u\n                                    )\n\n                                    motion_stim.pos = (\n                                        dx * scalar,\n                                        dy * scalar\n                                    )\n\n                                    # Baseline/ISI are neutral gray, but the\n                                    # stimulus background is user-defined.\n                                    # Reapply it explicitly on every frame.\n                                    fullfield.fillColor = background_rgb\n                                    fullfield.lineColor = background_rgb\n                                    fullfield.draw()\n                                    motion_stim.draw()\n                                    flip_and_log(\n                                        "motion",\n                                        trial,\n                                        condition,\n                                        stage_name,\n                                        f\n                                    )\n\n                                solid_frames(\n                                    GRAY,\n                                    isi_f,\n                                    "motion",\n                                    trial,\n                                    condition,\n                                    "inter_trial_gray"\n                                )\n\n                                write_json_atomic(\n                                    status_path,\n                                    {\n                                        "state":\n                                            "running",\n                                        "message":\n                                            f"{mode} · "\n                                            f"rep {rep}/{reps} · "\n                                            f"{direction:g}°",\n                                        "hz": hz,\n                                        "command_id":\n                                            command_id,\n                                        "details":\n                                            details\n                                    }\n                                )\n\n                    elif mode in (\n                        "Looming",\n                        "Receding",\n                    ):\n                        small_diameter = float(\n                            cfg[\n                                "motion_small_diameter_deg"\n                            ]\n                        )\n                        large_diameter = float(\n                            cfg[\n                                "motion_large_diameter_deg"\n                            ]\n                        )\n                        center_x = float(\n                            cfg[\n                                "motion_center_x_deg"\n                            ]\n                        )\n                        center_y = float(\n                            cfg[\n                                "motion_center_y_deg"\n                            ]\n                        )\n\n                        if (\n                            small_diameter <= 0\n                            or large_diameter\n                            <= small_diameter\n                        ):\n                            raise ValueError(\n                                "Motion: diámetro grande > "\n                                "diámetro pequeño > 0."\n                            )\n\n                        details.update({\n                            "small_diameter_deg":\n                                small_diameter,\n                            "large_diameter_deg":\n                                large_diameter,\n                            "center_x_deg":\n                                center_x,\n                            "center_y_deg":\n                                center_y,\n                        })\n\n                        if motion_stim is None:\n                            motion_stim = visual.Circle(\n                                win=win,\n                                radius=(\n                                    large_diameter\n                                    / 2.0\n                                ),\n                                units="deg",\n                                pos=(\n                                    center_x,\n                                    center_y\n                                ),\n                                fillColor=stimulus_rgb,\n                                lineColor=stimulus_rgb,\n                                colorSpace="rgb"\n                            )\n\n                        motion_stim.pos = (\n                            center_x,\n                            center_y\n                        )\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            trial += 1\n                            condition = mode\n\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "motion",\n                                trial,\n                                condition,\n                                "baseline_gray"\n                            )\n\n                            if mode == "Looming":\n                                ttl_name = (\n                                    "LOOMING_START"\n                                )\n                                stage_name = (\n                                    "looming"\n                                )\n                            else:\n                                ttl_name = (\n                                    "RECEDING_START"\n                                )\n                                stage_name = (\n                                    "receding"\n                                )\n\n                            queue_ttl_event(\n                                ttl_name,\n                                trial,\n                                condition\n                            )\n\n                            for f in range(\n                                1, stim_f + 1\n                            ):\n                                check_abort()\n\n                                u = (\n                                    0.0\n                                    if stim_f <= 1\n                                    else (\n                                        (f - 1)\n                                        / (stim_f - 1)\n                                    )\n                                )\n\n                                if mode == "Looming":\n                                    diameter = (\n                                        small_diameter\n                                        + (\n                                            large_diameter\n                                            - small_diameter\n                                        )\n                                        * u\n                                    )\n                                else:\n                                    diameter = (\n                                        large_diameter\n                                        - (\n                                            large_diameter\n                                            - small_diameter\n                                        )\n                                        * u\n                                    )\n\n                                motion_stim.radius = (\n                                    diameter\n                                    / 2.0\n                                )\n\n                                fullfield.fillColor = background_rgb\n                                fullfield.lineColor = background_rgb\n                                fullfield.draw()\n                                motion_stim.draw()\n                                flip_and_log(\n                                    "motion",\n                                    trial,\n                                    condition,\n                                    stage_name,\n                                    f\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                isi_f,\n                                "motion",\n                                trial,\n                                condition,\n                                "inter_trial_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"{mode} · "\n                                        f"rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    elif mode in (\n                        "Expanding Annulus",\n                        "Receding Annulus",\n                    ):\n                        small_diameter = float(\n                            cfg.get(\n                                "motion_annulus_small_diameter_deg",\n                                8.0\n                            )\n                        )\n                        large_diameter = float(\n                            cfg.get(\n                                "motion_annulus_large_diameter_deg",\n                                40.0\n                            )\n                        )\n                        thickness = float(\n                            cfg.get(\n                                "motion_annulus_thickness_deg",\n                                2.0\n                            )\n                        )\n                        center_x = float(\n                            cfg[\n                                "motion_center_x_deg"\n                            ]\n                        )\n                        center_y = float(\n                            cfg[\n                                "motion_center_y_deg"\n                            ]\n                        )\n\n                        if (\n                            small_diameter <= 0\n                            or large_diameter\n                            <= small_diameter\n                        ):\n                            raise ValueError(\n                                "Annulus: diámetro exterior grande > "\n                                "diámetro exterior pequeño > 0."\n                            )\n\n                        if thickness <= 0:\n                            raise ValueError(\n                                "Annulus: el grosor radial debe ser > 0."\n                            )\n\n                        if (\n                            2.0 * thickness\n                            >= small_diameter\n                        ):\n                            raise ValueError(\n                                "Annulus: el grosor radial debe ser menor "\n                                "que la mitad del diámetro exterior pequeño."\n                            )\n\n                        actual_duration_s = (\n                            stim_f / hz\n                        )\n\n                        details.update({\n                            "small_outer_diameter_deg":\n                                small_diameter,\n                            "large_outer_diameter_deg":\n                                large_diameter,\n                            "annulus_thickness_deg":\n                                thickness,\n                            "small_inner_diameter_deg":\n                                (\n                                    small_diameter\n                                    - 2.0 * thickness\n                                ),\n                            "large_inner_diameter_deg":\n                                (\n                                    large_diameter\n                                    - 2.0 * thickness\n                                ),\n                            "center_x_deg":\n                                center_x,\n                            "center_y_deg":\n                                center_y,\n                            "diameter_interpolation":\n                                "linear_outer_diameter",\n                            "thickness_definition":\n                                "constant_radial_thickness",\n                            "outer_diameter_speed_deg_s":\n                                (\n                                    (\n                                        large_diameter\n                                        - small_diameter\n                                    )\n                                    / actual_duration_s\n                                ),\n                            "radial_edge_speed_deg_s":\n                                (\n                                    (\n                                        large_diameter\n                                        - small_diameter\n                                    )\n                                    / 2.0\n                                    / actual_duration_s\n                                ),\n                        })\n\n                        if motion_stim is None:\n                            motion_stim = visual.Circle(\n                                win=win,\n                                radius=large_diameter / 2.0,\n                                units="deg",\n                                pos=(\n                                    center_x,\n                                    center_y\n                                ),\n                                fillColor=stimulus_rgb,\n                                lineColor=stimulus_rgb,\n                                colorSpace="rgb"\n                            )\n\n                        if motion_inner_stim is None:\n                            motion_inner_stim = visual.Circle(\n                                win=win,\n                                radius=(\n                                    (\n                                        large_diameter\n                                        - 2.0 * thickness\n                                    )\n                                    / 2.0\n                                ),\n                                units="deg",\n                                pos=(\n                                    center_x,\n                                    center_y\n                                ),\n                                fillColor=background_rgb,\n                                lineColor=background_rgb,\n                                colorSpace="rgb"\n                            )\n\n                        motion_stim.pos = (\n                            center_x,\n                            center_y\n                        )\n                        motion_inner_stim.pos = (\n                            center_x,\n                            center_y\n                        )\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            trial += 1\n                            condition = mode\n\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "motion",\n                                trial,\n                                condition,\n                                "baseline_gray"\n                            )\n\n                            if mode == "Expanding Annulus":\n                                ttl_name = (\n                                    "EXPANDING_ANNULUS_START"\n                                )\n                                stage_name = (\n                                    "expanding_annulus"\n                                )\n                            else:\n                                ttl_name = (\n                                    "RECEDING_ANNULUS_START"\n                                )\n                                stage_name = (\n                                    "receding_annulus"\n                                )\n\n                            queue_ttl_event(\n                                ttl_name,\n                                trial,\n                                condition\n                            )\n\n                            for f in range(\n                                1, stim_f + 1\n                            ):\n                                check_abort()\n\n                                u = (\n                                    0.0\n                                    if stim_f <= 1\n                                    else (\n                                        (f - 1)\n                                        / (stim_f - 1)\n                                    )\n                                )\n\n                                if mode == "Expanding Annulus":\n                                    outer_diameter = (\n                                        small_diameter\n                                        + (\n                                            large_diameter\n                                            - small_diameter\n                                        )\n                                        * u\n                                    )\n                                else:\n                                    outer_diameter = (\n                                        large_diameter\n                                        - (\n                                            large_diameter\n                                            - small_diameter\n                                        )\n                                        * u\n                                    )\n\n                                inner_diameter = (\n                                    outer_diameter\n                                    - 2.0 * thickness\n                                )\n\n                                motion_stim.radius = (\n                                    outer_diameter / 2.0\n                                )\n                                motion_inner_stim.radius = (\n                                    inner_diameter / 2.0\n                                )\n\n                                fullfield.fillColor = background_rgb\n                                fullfield.lineColor = background_rgb\n                                fullfield.draw()\n                                motion_stim.draw()\n                                motion_inner_stim.draw()\n\n                                flip_and_log(\n                                    "motion",\n                                    trial,\n                                    condition,\n                                    stage_name,\n                                    f\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                isi_f,\n                                "motion",\n                                trial,\n                                condition,\n                                "inter_trial_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"{mode} · "\n                                        f"rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    else:\n                        raise ValueError(\n                            f"Modo Motion desconocido: "\n                            f"{mode}"\n                        )\n\n                # ==========================================================\n                # GRATING\n                # ==========================================================\n                elif stimulus_type == "grating":\n                    mode = str(\n                        cfg.get(\n                            "grating_mode",\n                            "Drifting"\n                        )\n                    )\n                    if mode == "Gabor":\n                        mode = "Static Gabor"\n\n                    baseline_f = seconds_to_frames(\n                        cfg[\n                            "grating_baseline_s"\n                        ],\n                        hz\n                    )\n                    stim_f = seconds_to_frames(\n                        cfg[\n                            "grating_duration_s"\n                        ],\n                        hz\n                    )\n                    isi_f = seconds_to_frames(\n                        cfg[\n                            "grating_isi_s"\n                        ],\n                        hz\n                    )\n                    reps = int(\n                        cfg[\n                            "grating_repetitions"\n                        ]\n                    )\n\n                    angles_text = cfg.get(\n                        "grating_angles",\n                        cfg.get(\n                            "grating_directions",\n                            ""\n                        )\n                    )\n                    angles = parse_float_list(\n                        angles_text\n                    )\n\n                    sf = float(\n                        cfg[\n                            "grating_sf_cpd"\n                        ]\n                    )\n                    tf = float(\n                        cfg.get(\n                            "grating_tf_hz",\n                            1.0\n                        )\n                    )\n                    reversal_hz = float(\n                        cfg.get(\n                            "grating_reversal_hz",\n                            2.0\n                        )\n                    )\n                    phase_initial = float(\n                        cfg.get(\n                            "grating_phase_cycles",\n                            0.0\n                        )\n                    )\n\n                    adjustable_contrast = float(\n                        cfg[\n                            "grating_contrast"\n                        ]\n                    )\n                    absolute = bool(\n                        cfg[\n                            "grating_absolute_contrast"\n                        ]\n                    )\n                    waveform = str(\n                        cfg[\n                            "grating_waveform"\n                        ]\n                    )\n                    asymmetric_polarity = str(\n                        cfg.get(\n                            "grating_asymmetric_polarity",\n                            "A_TO_B"\n                        )\n                    ).strip().upper()\n                    if asymmetric_polarity not in (\n                        "A_TO_B",\n                        "B_TO_A",\n                    ):\n                        raise ValueError(\n                            "Asymmetric Drift: polaridad desconocida."\n                        )\n\n                    if not angles:\n                        raise ValueError(\n                            "Grating: no hay direcciones/orientaciones."\n                        )\n                    if sf <= 0:\n                        raise ValueError(\n                            "Grating: frecuencia espacial debe ser > 0."\n                        )\n                    if reps < 1:\n                        raise ValueError(\n                            "Grating: repeticiones debe ser >= 1."\n                        )\n                    if not (\n                        0.0\n                        <= adjustable_contrast\n                        <= 1.0\n                    ):\n                        raise ValueError(\n                            "Grating: contraste debe estar entre 0 y 1."\n                        )\n\n                    if (\n                        mode in (\n                            "Drifting",\n                            "Drifting Gabor",\n                            "Asymmetric Drift",\n                        )\n                        and tf <= 0\n                    ):\n                        raise ValueError(\n                            f"{mode}: frecuencia temporal debe ser > 0."\n                        )\n\n                    if (\n                        mode == "Phase Reversal"\n                        and reversal_hz <= 0\n                    ):\n                        raise ValueError(\n                            "Phase Reversal: frecuencia de inversión "\n                            "debe ser > 0."\n                        )\n\n                    contrast_used = (\n                        1.0\n                        if absolute\n                        else adjustable_contrast\n                    )\n\n                    gabor_size = float(\n                        cfg.get(\n                            "grating_gabor_size_deg",\n                            30.0\n                        )\n                    )\n                    gabor_sigma = float(\n                        cfg.get(\n                            "grating_gabor_sigma_deg",\n                            6.0\n                        )\n                    )\n                    gabor_x = float(\n                        cfg.get(\n                            "grating_gabor_x_deg",\n                            0.0\n                        )\n                    )\n                    gabor_y = float(\n                        cfg.get(\n                            "grating_gabor_y_deg",\n                            0.0\n                        )\n                    )\n\n                    if mode in (\n                        "Static Gabor",\n                        "Drifting Gabor",\n                    ):\n                        if (\n                            gabor_size <= 0\n                            or gabor_sigma <= 0\n                            or gabor_sigma\n                            > gabor_size / 2.0\n                        ):\n                            raise ValueError(\n                                f"{mode}: tamaño > 0 y "\n                                "0 < sigma <= tamaño/2."\n                            )\n\n                    if (\n                        prepared_step is not None\n                        and prepared_step.get(\n                            "stimulus_type"\n                        ) == "grating"\n                        and "grating"\n                        in prepared_step\n                    ):\n                        texture = prepared_step[\n                            "texture"\n                        ]\n                        grating = prepared_step[\n                            "grating"\n                        ]\n                    else:\n                        if mode == "Asymmetric Drift":\n                            texture = asymmetric_grating_texture(\n                                cfg[\n                                    "grating_color_a_rgb"\n                                ],\n                                cfg[\n                                    "grating_color_b_rgb"\n                                ],\n                                contrast_used,\n                                asymmetric_polarity\n                            )\n                        else:\n                            texture = colored_grating_texture(\n                                cfg[\n                                    "grating_color_a_rgb"\n                                ],\n                                cfg[\n                                    "grating_color_b_rgb"\n                                ],\n                                waveform,\n                                contrast_used\n                            )\n\n                        if mode in (\n                            "Static Gabor",\n                            "Drifting Gabor",\n                        ):\n                            grat_size = gabor_size\n                            mask = gaussian_mask_deg(\n                                gabor_size,\n                                gabor_sigma\n                            )\n                            pos = (\n                                gabor_x,\n                                gabor_y\n                            )\n                        else:\n                            grat_size = (\n                                2.2\n                                * math.hypot(\n                                    width_deg,\n                                    height_deg\n                                )\n                            )\n                            mask = None\n                            pos = (0, 0)\n\n                        grating = visual.GratingStim(\n                            win=win,\n                            tex=texture,\n                            mask=mask,\n                            units="deg",\n                            size=(\n                                grat_size,\n                                grat_size\n                            ),\n                            sf=sf,\n                            contrast=1.0,\n                            pos=pos,\n                            color=(1, 1, 1),\n                            colorSpace="rgb",\n                            interpolate=True\n                        )\n\n                    if mode in (\n                        "Static Gabor",\n                        "Drifting Gabor",\n                    ):\n                        gabor_bg_rgb_255 = cfg.get(\n                            "grating_gabor_background_rgb",\n                            [128, 128, 128]\n                        )\n                        gabor_bg = rgb255_to_psychopy(\n                            gabor_bg_rgb_255\n                        )\n                        grating.pos = (\n                            gabor_x,\n                            gabor_y\n                        )\n                    else:\n                        gabor_bg_rgb_255 = None\n                        gabor_bg = None\n                        grating.pos = (0, 0)\n\n                    effective_s = (\n                        stim_f / hz\n                        if stim_f\n                        else 0.0\n                    )\n\n                    details = {\n                        "mode": mode,\n                        "stimulus_frames":\n                            stim_f,\n                        "requested_duration_s":\n                            float(\n                                cfg[\n                                    "grating_duration_s"\n                                ]\n                            ),\n                        "effective_duration_s":\n                            effective_s,\n                        "angles":\n                            angles,\n                        "sf_cpd":\n                            sf,\n                        "tf_hz":\n                            (\n                                tf\n                                if mode in (\n                                    "Drifting",\n                                    "Drifting Gabor",\n                                    "Asymmetric Drift",\n                                )\n                                else None\n                            ),\n                        "reversal_hz":\n                            (\n                                reversal_hz\n                                if mode\n                                == "Phase Reversal"\n                                else None\n                            ),\n                        "phase_cycles":\n                            phase_initial,\n                        "contrast_used":\n                            contrast_used,\n                        "absolute_contrast":\n                            absolute,\n                        "waveform":\n                            waveform,\n                        "color_a_rgb_255":\n                            cfg[\n                                "grating_color_a_rgb"\n                            ],\n                        "color_b_rgb_255":\n                            cfg[\n                                "grating_color_b_rgb"\n                            ],\n                        "asymmetric_polarity":\n                            (\n                                asymmetric_polarity\n                                if mode == "Asymmetric Drift"\n                                else None\n                            ),\n                        "asymmetric_profile":\n                            (\n                                "half_cosine_smooth_transition_plus_abrupt_reset"\n                                if mode == "Asymmetric Drift"\n                                else None\n                            ),\n                    }\n\n                    if mode in (\n                        "Drifting",\n                        "Drifting Gabor",\n                        "Asymmetric Drift",\n                    ):\n                        details[\n                            "angle_semantics"\n                        ] = (\n                            "movement_direction_deg; "\n                            "0=izquierda→derecha; "\n                            "positivo antihorario"\n                        )\n                        details[\n                            "phase_cycles_per_frame"\n                        ] = (\n                            tf / hz\n                        )\n                    else:\n                        details[\n                            "angle_semantics"\n                        ] = (\n                            "bar_orientation_deg; "\n                            "0=barras verticales; "\n                            "positivo antihorario"\n                        )\n\n                    if mode == "Phase Reversal":\n                        details[\n                            "expected_frames_per_reversal"\n                        ] = (\n                            hz\n                            / reversal_hz\n                        )\n\n                    if mode in (\n                        "Static Gabor",\n                        "Drifting Gabor",\n                    ):\n                        details.update({\n                            "gabor_size_deg":\n                                gabor_size,\n                            "gabor_sigma_deg":\n                                gabor_sigma,\n                            "gabor_x_deg":\n                                gabor_x,\n                            "gabor_y_deg":\n                                gabor_y,\n                            "gabor_background_rgb_255":\n                                gabor_bg_rgb_255,\n                        })\n\n                    trial = 0\n\n                    for rep in range(\n                        1,\n                        reps + 1\n                    ):\n                        for angle in angles:\n                            trial += 1\n\n                            if mode == "Asymmetric Drift":\n                                condition = (\n                                    f"{mode} {angle:g}° "\n                                    f"{asymmetric_polarity}"\n                                )\n                            else:\n                                condition = (\n                                    f"{mode} "\n                                    f"{angle:g}°"\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "grating",\n                                trial,\n                                condition,\n                                "baseline_gray"\n                            )\n\n                            # PsychoPy positive ori is clockwise.\n                            # UI convention is CCW.\n                            grating.ori = -angle\n\n                            if mode == "Drifting":\n                                phase_value = 0.0\n                                phase_per_frame = (\n                                    tf / hz\n                                )\n                                ttl_name = (\n                                    "GRATING_DRIFT_START"\n                                )\n                                stage_name = (\n                                    "drifting_grating"\n                                )\n\n                            elif mode == "Asymmetric Drift":\n                                phase_value = 0.0\n                                phase_per_frame = (\n                                    tf / hz\n                                )\n                                grating.phase = (\n                                    phase_value,\n                                    0.0\n                                )\n                                ttl_name = (\n                                    "GRATING_ASYMMETRIC_START"\n                                )\n                                stage_name = (\n                                    "asymmetric_grating"\n                                )\n\n                            elif mode == "Static":\n                                grating.phase = (\n                                    phase_initial,\n                                    0.0\n                                )\n                                ttl_name = (\n                                    "GRATING_STATIC_START"\n                                )\n                                stage_name = (\n                                    "static_grating"\n                                )\n\n                            elif mode == "Phase Reversal":\n                                ttl_name = (\n                                    "GRATING_PHASE_REVERSAL_START"\n                                )\n                                stage_name = (\n                                    "phase_reversal_grating"\n                                )\n\n                            elif mode == "Static Gabor":\n                                grating.phase = (\n                                    phase_initial,\n                                    0.0\n                                )\n                                ttl_name = (\n                                    "GABOR_STATIC_START"\n                                )\n                                stage_name = (\n                                    "static_gabor"\n                                )\n\n                            elif mode == "Drifting Gabor":\n                                phase_value = (\n                                    phase_initial\n                                )\n                                phase_per_frame = (\n                                    tf / hz\n                                )\n                                grating.phase = (\n                                    phase_value,\n                                    0.0\n                                )\n                                ttl_name = (\n                                    "GABOR_DRIFT_START"\n                                )\n                                stage_name = (\n                                    "drifting_gabor"\n                                )\n\n                            else:\n                                raise ValueError(\n                                    f"Modo Grating desconocido: {mode}"\n                                )\n\n                            if stim_f > 0:\n                                queue_ttl_event(\n                                    ttl_name,\n                                    trial,\n                                    condition\n                                )\n\n                            for f in range(\n                                1,\n                                stim_f + 1\n                            ):\n                                check_abort()\n\n                                if mode in (\n                                    "Drifting",\n                                    "Drifting Gabor",\n                                    "Asymmetric Drift",\n                                ):\n                                    phase_value += (\n                                        phase_per_frame\n                                    )\n                                    grating.phase = (\n                                        phase_value,\n                                        0.0\n                                    )\n\n                                elif mode == "Phase Reversal":\n                                    reversal_index = int(\n                                        math.floor(\n                                            (\n                                                (f - 1)\n                                                * reversal_hz\n                                            )\n                                            / hz\n                                        )\n                                    )\n                                    phase_value = (\n                                        phase_initial\n                                        + (\n                                            0.5\n                                            if (\n                                                reversal_index\n                                                % 2\n                                            )\n                                            else 0.0\n                                        )\n                                    )\n                                    grating.phase = (\n                                        phase_value,\n                                        0.0\n                                    )\n\n                                if mode in (\n                                    "Static Gabor",\n                                    "Drifting Gabor",\n                                ):\n                                    fullfield.fillColor = (\n                                        gabor_bg\n                                    )\n                                    fullfield.lineColor = (\n                                        gabor_bg\n                                    )\n                                    fullfield.draw()\n\n                                grating.draw()\n\n                                flip_and_log(\n                                    "grating",\n                                    trial,\n                                    condition,\n                                    stage_name,\n                                    f\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                isi_f,\n                                "grating",\n                                trial,\n                                condition,\n                                "inter_trial_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"{mode} · "\n                                        f"rep {rep}/{reps} · "\n                                        f"{angle:g}°",\n                                    "hz": hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                # ==========================================================\n                # NOISE\n                # ==========================================================\n                elif stimulus_type == "dense_noise":\n                    baseline_f = seconds_to_frames(\n                        cfg[\n                            "dense_baseline_s"\n                        ],\n                        hz\n                    )\n                    stim_total_f = seconds_to_frames(\n                        cfg[\n                            "dense_duration_s"\n                        ],\n                        hz\n                    )\n                    isi_f = seconds_to_frames(\n                        cfg[\n                            "dense_isi_s"\n                        ],\n                        hz\n                    )\n                    reps = int(\n                        cfg[\n                            "dense_repetitions"\n                        ]\n                    )\n                    update_hz_req = float(\n                        cfg[\n                            "dense_update_hz"\n                        ]\n                    )\n                    check_size_deg = float(\n                        cfg[\n                            "dense_check_size_deg"\n                        ]\n                    )\n                    contrast = float(\n                        cfg[\n                            "dense_contrast"\n                        ]\n                    )\n                    seed_base = int(\n                        cfg[\n                            "dense_seed"\n                        ]\n                    )\n\n                    if (\n                        prepared_step is not None\n                        and prepared_step.get(\n                            "stimulus_type"\n                        ) == "dense_noise"\n                        and "noise_stim"\n                        in prepared_step\n                    ):\n                        resources = (\n                            prepared_step\n                        )\n                    else:\n                        resources = (\n                            build_noise_resources(\n                                cfg\n                            )\n                        )\n\n                    family_mode = resources[\n                        "family_mode"\n                    ]\n                    distribution = resources[\n                        "distribution"\n                    ]\n                    frozen = bool(\n                        resources[\n                            "frozen"\n                        ]\n                    )\n                    frames_per_update = resources[\n                        "frames_per_update"\n                    ]\n                    actual_update_hz = resources[\n                        "actual_update_hz"\n                    ]\n                    n_updates = resources[\n                        "n_updates"\n                    ]\n                    n_cols = resources[\n                        "n_cols"\n                    ]\n                    n_rows = resources[\n                        "n_rows"\n                    ]\n                    noise_size_deg = resources[\n                        "noise_size_deg"\n                    ]\n                    state_sequences = resources[\n                        "state_sequences"\n                    ]\n                    image_sequences = resources[\n                        "image_sequences"\n                    ]\n                    used_seeds = resources[\n                        "used_seeds"\n                    ]\n                    noise_stim = resources[\n                        "noise_stim"\n                    ]\n\n                    sequence_file = resources.get(\n                        "sequence_file"\n                    )\n                    if not sequence_file:\n                        sequence_file = (\n                            save_noise_sequence_before_timing(\n                                resources,\n                                cfg\n                            )\n                        )\n\n                    distribution_label = (\n                        distribution.capitalize()\n                    )\n\n                    if (\n                        family_mode\n                        == "Dense White Noise"\n                    ):\n                        condition_name = (\n                            f"Dense White Noise "\n                            f"({distribution_label})"\n                        )\n                        ttl_name = (\n                            "DENSE_WHITE_NOISE_START"\n                        )\n\n                    elif (\n                        family_mode\n                        == "Gaussian White Noise"\n                    ):\n                        condition_name = (\n                            "Gaussian White Noise"\n                        )\n                        ttl_name = (\n                            "GAUSSIAN_WHITE_NOISE_START"\n                        )\n\n                    else:\n                        condition_name = (\n                            f"Frozen Noise "\n                            f"({distribution_label})"\n                        )\n                        ttl_name = (\n                            "FROZEN_NOISE_START"\n                        )\n\n                    details = {\n                        "family_mode":\n                            family_mode,\n                        "distribution":\n                            distribution_label,\n                        "frozen":\n                            frozen,\n                        "baseline_frames":\n                            baseline_f,\n                        "stimulus_total_frames":\n                            stim_total_f,\n                        "isi_frames":\n                            isi_f,\n                        "requested_update_hz":\n                            update_hz_req,\n                        "actual_update_hz":\n                            actual_update_hz,\n                        "update_limited_by_refresh":\n                            bool(\n                                update_hz_req\n                                > hz\n                            ),\n                        "frames_per_update":\n                            frames_per_update,\n                        "n_updates":\n                            n_updates,\n                        "check_size_deg":\n                            check_size_deg,\n                        "grid_rows":\n                            n_rows,\n                        "grid_cols":\n                            n_cols,\n                        "contrast":\n                            contrast,\n                        "seed_base":\n                            seed_base,\n                        "used_seeds":\n                            used_seeds,\n                        "color_low_rgb_255":\n                            cfg[\n                                "dense_color_low_rgb"\n                            ],\n                        "color_mid_rgb_255":\n                            cfg[\n                                "dense_color_mid_rgb"\n                            ],\n                        "color_high_rgb_255":\n                            cfg[\n                                "dense_color_high_rgb"\n                            ],\n                        "sequence_file":\n                            str(\n                                sequence_file\n                            ),\n                        "sequence_saved_before_timing":\n                            bool(\n                                resources.get(\n                                    "sequence_saved_before_timing",\n                                    True\n                                )\n                            ),\n                    }\n\n                    if distribution == "gaussian":\n                        details[\n                            "gaussian_mean"\n                        ] = (\n                            "color_mid"\n                        )\n                        details[\n                            "gaussian_sigma_normalized"\n                        ] = (\n                            contrast\n                            / 3.0\n                        )\n                        details[\n                            "gaussian_clip_normalized"\n                        ] = [\n                            -contrast,\n                            contrast\n                        ]\n                        details[\n                            "gaussian_color_mapping"\n                        ] = (\n                            "low≈-3σ; mid=mean; high≈+3σ "\n                            "when contrast=1"\n                        )\n\n                    for rep in range(\n                        1,\n                        reps + 1\n                    ):\n                        solid_frames(\n                            GRAY,\n                            baseline_f,\n                            "dense_noise",\n                            rep,\n                            condition_name,\n                            "baseline_gray"\n                        )\n\n                        rep_images = (\n                            image_sequences[\n                                rep - 1\n                            ]\n                        )\n\n                        frames_remaining = (\n                            stim_total_f\n                        )\n\n                        if stim_total_f > 0:\n                            queue_ttl_event(\n                                ttl_name,\n                                rep,\n                                condition_name\n                            )\n\n                        for upd_idx in range(\n                            n_updates\n                        ):\n                            noise_stim.image = (\n                                rep_images[\n                                    upd_idx\n                                ]\n                            )\n\n                            hold = min(\n                                frames_per_update,\n                                frames_remaining\n                            )\n                            frames_remaining -= (\n                                hold\n                            )\n\n                            for f in range(\n                                1,\n                                hold + 1\n                            ):\n                                check_abort()\n                                noise_stim.draw()\n                                flip_and_log(\n                                    "dense_noise",\n                                    rep,\n                                    condition_name,\n                                    (\n                                        f"noise_update_"\n                                        f"{upd_idx + 1}"\n                                    ),\n                                    f\n                                )\n\n                        solid_frames(\n                            GRAY,\n                            isi_f,\n                            "dense_noise",\n                            rep,\n                            condition_name,\n                            "inter_trial_gray"\n                        )\n\n                        write_json_atomic(\n                            status_path,\n                            {\n                                "state":\n                                    "running",\n                                "message":\n                                    f"{condition_name} · "\n                                    f"rep {rep}/{reps}",\n                                "hz":\n                                    hz,\n                                "command_id":\n                                    command_id,\n                                "details":\n                                    details\n                            }\n                        )\n\n                # ==========================================================\n                # RF MAPPING\n                # ==========================================================\n                elif stimulus_type == "rf_mapping":\n                    mode_raw = str(\n                        cfg["rf_mode"]\n                    ).strip()\n                    mode = mode_raw.lower()\n\n                    baseline_f = seconds_to_frames(\n                        cfg["rf_baseline_s"], hz\n                    )\n                    stim_f = seconds_to_frames(\n                        cfg["rf_duration_s"], hz\n                    )\n                    isi_f = seconds_to_frames(\n                        cfg["rf_isi_s"], hz\n                    )\n                    reps = int(\n                        cfg["rf_repetitions"]\n                    )\n\n                    x_deg = float(\n                        cfg["rf_x_deg"]\n                    )\n                    y_deg = float(\n                        cfg["rf_y_deg"]\n                    )\n\n                    stim_rgb = rgb255_to_psychopy(\n                        cfg["rf_stimulus_rgb"]\n                    )\n                    bg_rgb = rgb255_to_psychopy(\n                        cfg["rf_background_rgb"]\n                    )\n\n                    if stim_f <= 0:\n                        raise ValueError(\n                            "RF Mapping: la duración debe producir al menos 1 frame."\n                        )\n                    if reps < 1:\n                        raise ValueError(\n                            "RF Mapping: repeticiones debe ser >= 1."\n                        )\n\n                    trial = 0\n                    details = {\n                        "mode": mode_raw,\n                        "baseline_frames": baseline_f,\n                        "stimulus_frames": stim_f,\n                        "isi_frames": isi_f,\n                        "repetitions": reps,\n                        "x_deg": x_deg,\n                        "y_deg": y_deg,\n                        "stimulus_rgb_255":\n                            cfg["rf_stimulus_rgb"],\n                        "background_rgb_255":\n                            cfg["rf_background_rgb"],\n                    }\n\n                    def present_rf_shape(\n                        draw_callable,\n                        condition_name,\n                        stage_name,\n                        ttl_name\n                    ):\n                        nonlocal trial\n                        trial += 1\n\n                        queue_ttl_event(\n                            ttl_name,\n                            trial,\n                            condition_name\n                        )\n\n                        for frame_i in range(\n                            1, stim_f + 1\n                        ):\n                            check_abort()\n                            fullfield.fillColor = bg_rgb\n                            fullfield.lineColor = bg_rgb\n                            fullfield.draw()\n                            draw_callable()\n                            flip_and_log(\n                                "rf_mapping",\n                                trial,\n                                condition_name,\n                                stage_name,\n                                frame_i\n                            )\n\n                        solid_frames(\n                            GRAY,\n                            isi_f,\n                            "rf_mapping",\n                            trial,\n                            condition_name,\n                            "inter_condition_gray"\n                        )\n\n                    if mode == "spot":\n                        diameter = float(\n                            cfg["rf_spot_diameter_deg"]\n                        )\n                        if diameter <= 0:\n                            raise ValueError(\n                                "RF Spot: diámetro debe ser > 0."\n                            )\n\n                        details[\n                            "spot_diameter_deg"\n                        ] = diameter\n\n                        if (\n                            prepared_step is not None\n                            and prepared_step.get(\n                                "stimulus_type"\n                            ) == "rf_mapping"\n                            and prepared_step.get(\n                                "rf_mode"\n                            ) == "spot"\n                            and "spot"\n                            in prepared_step\n                        ):\n                            spot = prepared_step[\n                                "spot"\n                            ]\n                        else:\n                            spot = visual.Circle(\n                                win=win,\n                                radius=diameter / 2.0,\n                                units="deg",\n                                pos=(x_deg, y_deg),\n                                fillColor=stim_rgb,\n                                lineColor=stim_rgb,\n                                colorSpace="rgb"\n                            )\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "rf_mapping",\n                                trial + 1,\n                                "Spot",\n                                "baseline_gray"\n                            )\n\n                            present_rf_shape(\n                                spot.draw,\n                                (\n                                    f"Spot {diameter:g}° "\n                                    f"X{x_deg:g} Y{y_deg:g}"\n                                ),\n                                "rf_spot",\n                                "RF_SPOT_START"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state": "running",\n                                    "message":\n                                        f"RF Spot · rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id": command_id,\n                                    "details": details\n                                }\n                            )\n\n                    elif mode == "annulus":\n                        inner = float(\n                            cfg["rf_annulus_inner_deg"]\n                        )\n                        outer = float(\n                            cfg["rf_annulus_outer_deg"]\n                        )\n                        if (\n                            inner <= 0\n                            or outer <= inner\n                        ):\n                            raise ValueError(\n                                "RF Annulus: externo > interno > 0."\n                            )\n\n                        details[\n                            "annulus_inner_deg"\n                        ] = inner\n                        details[\n                            "annulus_outer_deg"\n                        ] = outer\n\n                        if (\n                            prepared_step is not None\n                            and prepared_step.get(\n                                "stimulus_type"\n                            ) == "rf_mapping"\n                            and prepared_step.get(\n                                "rf_mode"\n                            ) == "annulus"\n                            and "outer_circle"\n                            in prepared_step\n                        ):\n                            outer_circle = prepared_step[\n                                "outer_circle"\n                            ]\n                            inner_circle = prepared_step[\n                                "inner_circle"\n                            ]\n                        else:\n                            outer_circle = visual.Circle(\n                                win=win,\n                                radius=outer / 2.0,\n                                units="deg",\n                                pos=(x_deg, y_deg),\n                                fillColor=stim_rgb,\n                                lineColor=stim_rgb,\n                                colorSpace="rgb"\n                            )\n                            inner_circle = visual.Circle(\n                                win=win,\n                                radius=inner / 2.0,\n                                units="deg",\n                                pos=(x_deg, y_deg),\n                                fillColor=bg_rgb,\n                                lineColor=bg_rgb,\n                                colorSpace="rgb"\n                            )\n\n                        def draw_annulus():\n                            outer_circle.draw()\n                            inner_circle.draw()\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "rf_mapping",\n                                trial + 1,\n                                "Annulus",\n                                "baseline_gray"\n                            )\n\n                            present_rf_shape(\n                                draw_annulus,\n                                (\n                                    f"Annulus {inner:g}-{outer:g}° "\n                                    f"X{x_deg:g} Y{y_deg:g}"\n                                ),\n                                "rf_annulus",\n                                "RF_ANNULUS_START"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state": "running",\n                                    "message":\n                                        f"RF Annulus · rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id": command_id,\n                                    "details": details\n                                }\n                            )\n\n                    elif mode == "size series":\n                        sizes = parse_float_list(\n                            cfg["rf_size_series_deg"]\n                        )\n                        if not sizes:\n                            raise ValueError(\n                                "RF Size Series: no hay diámetros."\n                            )\n                        if any(v <= 0 for v in sizes):\n                            raise ValueError(\n                                "RF Size Series: todos los diámetros deben ser > 0."\n                            )\n\n                        details[\n                            "size_series_deg"\n                        ] = sizes\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "rf_mapping",\n                                trial + 1,\n                                "Size series",\n                                "baseline_gray"\n                            )\n\n                            prepared_size_spots = None\n                            if (\n                                prepared_step is not None\n                                and prepared_step.get(\n                                    "stimulus_type"\n                                ) == "rf_mapping"\n                                and prepared_step.get(\n                                    "rf_mode"\n                                ) == "size series"\n                            ):\n                                prepared_size_spots = (\n                                    prepared_step.get(\n                                        "size_spots"\n                                    )\n                                )\n\n                            for size_idx, diameter in enumerate(\n                                sizes\n                            ):\n                                if (\n                                    prepared_size_spots is not None\n                                    and size_idx\n                                    < len(\n                                        prepared_size_spots\n                                    )\n                                ):\n                                    spot = (\n                                        prepared_size_spots[\n                                            size_idx\n                                        ]\n                                    )\n                                else:\n                                    spot = visual.Circle(\n                                        win=win,\n                                        radius=diameter / 2.0,\n                                        units="deg",\n                                        pos=(x_deg, y_deg),\n                                        fillColor=stim_rgb,\n                                        lineColor=stim_rgb,\n                                        colorSpace="rgb"\n                                    )\n\n                                present_rf_shape(\n                                    spot.draw,\n                                    (\n                                        f"Size {diameter:g}° "\n                                        f"X{x_deg:g} Y{y_deg:g}"\n                                    ),\n                                    "rf_size_series",\n                                    "RF_SIZE_START"\n                                )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state": "running",\n                                    "message":\n                                        f"RF Size Series · rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id": command_id,\n                                    "details": details\n                                }\n                            )\n\n                    elif mode == "annulus size series":\n                        sizes = parse_float_list(\n                            cfg[\n                                "rf_annulus_size_series_deg"\n                            ]\n                        )\n                        thickness = float(\n                            cfg[\n                                "rf_annulus_series_thickness_deg"\n                            ]\n                        )\n\n                        if not sizes:\n                            raise ValueError(\n                                "RF Annulus Size Series: no hay diámetros."\n                            )\n                        if any(\n                            diameter <= 0\n                            for diameter in sizes\n                        ):\n                            raise ValueError(\n                                "RF Annulus Size Series: todos los diámetros deben ser > 0."\n                            )\n                        if thickness <= 0:\n                            raise ValueError(\n                                "RF Annulus Size Series: el grosor radial debe ser > 0."\n                            )\n                        if any(\n                            2.0 * thickness >= diameter\n                            for diameter in sizes\n                        ):\n                            raise ValueError(\n                                "RF Annulus Size Series: grosor incompatible "\n                                "con al menos un diámetro."\n                            )\n\n                        details.update({\n                            "annulus_size_series_deg":\n                                sizes,\n                            "annulus_series_thickness_deg":\n                                thickness,\n                            "annulus_inner_diameters_deg":\n                                [\n                                    diameter\n                                    - 2.0 * thickness\n                                    for diameter in sizes\n                                ],\n                            "size_conditions":\n                                len(sizes),\n                            "thickness_definition":\n                                "constant_radial_thickness",\n                        })\n\n                        prepared_pairs = None\n\n                        if (\n                            prepared_step is not None\n                            and prepared_step.get(\n                                "stimulus_type"\n                            ) == "rf_mapping"\n                            and prepared_step.get(\n                                "rf_mode"\n                            ) == "annulus size series"\n                            and "annulus_size_pairs"\n                            in prepared_step\n                        ):\n                            prepared_pairs = prepared_step[\n                                "annulus_size_pairs"\n                            ]\n\n                        # Standalone fallback if, for any reason, this\n                        # stimulus was executed without the normal preload.\n                        # The standard GUI/protocol path should use the\n                        # already-prewarmed pairs above.\n                        if prepared_pairs is None:\n                            prepared_pairs = []\n\n                            for diameter in sizes:\n                                inner_diameter = (\n                                    diameter\n                                    - 2.0 * thickness\n                                )\n\n                                fallback_outer = visual.Circle(\n                                    win=win,\n                                    radius=diameter / 2.0,\n                                    units="deg",\n                                    pos=(x_deg, y_deg),\n                                    fillColor=stim_rgb,\n                                    lineColor=stim_rgb,\n                                    colorSpace="rgb"\n                                )\n                                fallback_inner = visual.Circle(\n                                    win=win,\n                                    radius=inner_diameter / 2.0,\n                                    units="deg",\n                                    pos=(x_deg, y_deg),\n                                    fillColor=bg_rgb,\n                                    lineColor=bg_rgb,\n                                    colorSpace="rgb"\n                                )\n\n                                prepared_pairs.append(\n                                    (\n                                        float(diameter),\n                                        fallback_outer,\n                                        fallback_inner,\n                                    )\n                                )\n\n                        details[\n                            "all_sizes_prewarmed_before_timing"\n                        ] = (\n                            prepared_step is not None\n                            and "annulus_size_pairs"\n                            in prepared_step\n                        )\n                        details[\n                            "preloaded_annulus_objects"\n                        ] = len(\n                            prepared_pairs\n                        )\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "rf_mapping",\n                                trial + 1,\n                                "Annulus Size series",\n                                "baseline_gray"\n                            )\n\n                            for (\n                                diameter,\n                                outer_circle,\n                                inner_circle,\n                            ) in prepared_pairs:\n\n                                def draw_annulus_size(\n                                    outer=outer_circle,\n                                    inner=inner_circle,\n                                ):\n                                    outer.draw()\n                                    inner.draw()\n\n                                present_rf_shape(\n                                    draw_annulus_size,\n                                    (\n                                        f"Annulus Size {diameter:g}° "\n                                        f"X{x_deg:g} Y{y_deg:g}"\n                                    ),\n                                    "rf_annulus_size_series",\n                                    "RF_ANNULUS_SIZE_START"\n                                )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state": "running",\n                                    "message":\n                                        f"RF Annulus Size Series · "\n                                        f"rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id": command_id,\n                                    "details": details\n                                }\n                            )\n\n                    elif mode == "sparse noise":\n                        square_size = float(\n                            cfg["rf_sparse_square_deg"]\n                        )\n                        grid_step = float(\n                            cfg["rf_sparse_grid_step_deg"]\n                        )\n                        n_presentations = int(\n                            cfg["rf_sparse_presentations"]\n                        )\n                        polarity = str(\n                            cfg["rf_sparse_polarity"]\n                        ).strip().lower()\n                        seed_base = int(\n                            cfg["rf_sparse_seed"]\n                        )\n\n                        if (\n                            square_size <= 0\n                            or grid_step <= 0\n                        ):\n                            raise ValueError(\n                                "Sparse Noise: tamaño y paso deben ser > 0."\n                            )\n                        if n_presentations < 1:\n                            raise ValueError(\n                                "Sparse Noise: presentaciones debe ser >= 1."\n                            )\n                        if polarity not in (\n                            "bright", "dark", "both"\n                        ):\n                            raise ValueError(\n                                "Sparse Noise: polaridad no válida."\n                            )\n\n                        if (\n                            prepared_step is not None\n                            and prepared_step.get(\n                                "stimulus_type"\n                            ) == "rf_mapping"\n                            and prepared_step.get(\n                                "rf_mode"\n                            ) == "sparse noise"\n                            and "sparse"\n                            in prepared_step\n                        ):\n                            positions = prepared_step[\n                                "positions"\n                            ]\n                            sparse = prepared_step[\n                                "sparse"\n                            ]\n                        else:\n                            half = square_size / 2.0\n                            x_min = -width_deg / 2.0 + half\n                            x_max = width_deg / 2.0 - half\n                            y_min = -height_deg / 2.0 + half\n                            y_max = height_deg / 2.0 - half\n\n                            xs = np.arange(\n                                x_min,\n                                x_max + grid_step * 0.25,\n                                grid_step\n                            )\n                            ys = np.arange(\n                                y_min,\n                                y_max + grid_step * 0.25,\n                                grid_step\n                            )\n\n                            if len(xs) == 0:\n                                xs = np.asarray([0.0])\n                            if len(ys) == 0:\n                                ys = np.asarray([0.0])\n\n                            positions = [\n                                (float(x), float(y))\n                                for y in ys\n                                for x in xs\n                            ]\n\n                            sparse = visual.Rect(\n                                win=win,\n                                width=square_size,\n                                height=square_size,\n                                units="deg",\n                                fillColor=WHITE,\n                                lineColor=WHITE,\n                                colorSpace="rgb",\n                                pos=(0, 0)\n                            )\n\n                        details.update({\n                            "sparse_square_deg":\n                                square_size,\n                            "sparse_grid_step_deg":\n                                grid_step,\n                            "sparse_presentations_per_rep":\n                                n_presentations,\n                            "sparse_polarity":\n                                polarity,\n                            "sparse_seed":\n                                seed_base,\n                            "sparse_grid_positions":\n                                len(positions),\n                            "sparse_background":\n                                "gray_50_percent",\n                        })\n\n                        for rep in range(\n                            1, reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                baseline_f,\n                                "rf_mapping",\n                                trial + 1,\n                                "Sparse Noise",\n                                "baseline_gray"\n                            )\n\n                            rng = np.random.default_rng(\n                                seed_base + rep - 1\n                            )\n\n                            for presentation in range(\n                                1,\n                                n_presentations + 1\n                            ):\n                                pos_index = int(\n                                    rng.integers(\n                                        0,\n                                        len(positions)\n                                    )\n                                )\n                                px, py = positions[\n                                    pos_index\n                                ]\n\n                                if polarity == "bright":\n                                    level = "Bright"\n                                    square_color = WHITE\n                                elif polarity == "dark":\n                                    level = "Dark"\n                                    square_color = BLACK\n                                else:\n                                    if int(\n                                        rng.integers(0, 2)\n                                    ) == 1:\n                                        level = "Bright"\n                                        square_color = WHITE\n                                    else:\n                                        level = "Dark"\n                                        square_color = BLACK\n\n                                sparse.pos = (px, py)\n                                sparse.fillColor = square_color\n                                sparse.lineColor = square_color\n\n                                condition = (\n                                    f"{level} "\n                                    f"X{px:.3f} Y{py:.3f}"\n                                )\n\n                                trial += 1\n                                queue_ttl_event(\n                                    "RF_SPARSE_START",\n                                    trial,\n                                    condition\n                                )\n\n                                for frame_i in range(\n                                    1, stim_f + 1\n                                ):\n                                    check_abort()\n                                    fullfield.fillColor = GRAY\n                                    fullfield.lineColor = GRAY\n                                    fullfield.draw()\n                                    sparse.draw()\n                                    flip_and_log(\n                                        "rf_mapping",\n                                        trial,\n                                        condition,\n                                        "rf_sparse_noise",\n                                        frame_i\n                                    )\n\n                                solid_frames(\n                                    GRAY,\n                                    isi_f,\n                                    "rf_mapping",\n                                    trial,\n                                    condition,\n                                    "inter_condition_gray"\n                                )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state": "running",\n                                    "message":\n                                        f"RF Sparse Noise · rep {rep}/{reps}",\n                                    "hz": hz,\n                                    "command_id": command_id,\n                                    "details": details\n                                }\n                            )\n\n                    else:\n                        raise ValueError(\n                            f"Modo RF Mapping desconocido: {mode_raw}"\n                        )\n\n                # ==========================================================\n                # CHIRP + INTENSITY\n                # ==========================================================\n                elif stimulus_type == "chirp_intensity":\n                    mode = str(\n                        cfg.get(\n                            "temporal_mode",\n                            "Chirp + Intensity"\n                        )\n                    )\n\n                    basal_f = seconds_to_frames(\n                        cfg[\n                            "chirp_basal_s"\n                        ],\n                        hz\n                    )\n                    final_gray_f = seconds_to_frames(\n                        cfg[\n                            "chirp_final_gray_s"\n                        ],\n                        hz\n                    )\n                    reps = int(\n                        cfg[\n                            "chirp_repetitions"\n                        ]\n                    )\n\n                    if reps < 1:\n                        raise ValueError(\n                            "Temporal: repeticiones debe ser >= 1."\n                        )\n\n                    high_color = np.asarray(\n                        rgb255_to_psychopy(\n                            cfg[\n                                "chirp_on_color_rgb"\n                            ]\n                        ),\n                        dtype=np.float32\n                    )\n                    low_color = np.asarray(\n                        rgb255_to_psychopy(\n                            cfg[\n                                "chirp_off_color_rgb"\n                            ]\n                        ),\n                        dtype=np.float32\n                    )\n                    midpoint_color = (\n                        (\n                            high_color\n                            + low_color\n                        )\n                        / 2.0\n                    )\n                    half_range = (\n                        (\n                            high_color\n                            - low_color\n                        )\n                        / 2.0\n                    )\n\n                    if (\n                        prepared_step is not None\n                        and prepared_step.get(\n                            "stimulus_type"\n                        ) == "chirp_intensity"\n                        and prepared_step.get(\n                            "temporal_mode"\n                        ) == mode\n                    ):\n                        temporal_resources = (\n                            prepared_step\n                        )\n                    else:\n                        temporal_resources = (\n                            build_temporal_resources(\n                                cfg\n                            )\n                        )\n\n                    if mode == "Chirp + Intensity":\n                        step_on_f = seconds_to_frames(\n                            cfg[\n                                "chirp_step_on_s"\n                            ],\n                            hz\n                        )\n                        step_off_f = seconds_to_frames(\n                            cfg[\n                                "chirp_step_off_s"\n                            ],\n                            hz\n                        )\n                        pause1_f = seconds_to_frames(\n                            cfg[\n                                "chirp_pause1_s"\n                            ],\n                            hz\n                        )\n                        chirp_duration_f = seconds_to_frames(\n                            cfg[\n                                "chirp_frequency_duration_s"\n                            ],\n                            hz\n                        )\n                        pause2_f = seconds_to_frames(\n                            cfg[\n                                "chirp_pause2_s"\n                            ],\n                            hz\n                        )\n                        intensity_duration_f = seconds_to_frames(\n                            cfg[\n                                "chirp_intensity_duration_s"\n                            ],\n                            hz\n                        )\n\n                        f_min = float(\n                            cfg[\n                                "chirp_frequency_min_hz"\n                            ]\n                        )\n                        f_max = float(\n                            cfg[\n                                "chirp_frequency_max_hz"\n                            ]\n                        )\n                        intensity_freq = float(\n                            cfg[\n                                "chirp_intensity_frequency_hz"\n                            ]\n                        )\n                        intensity_start_pct = float(\n                            cfg[\n                                "chirp_intensity_start_pct"\n                            ]\n                        )\n\n                        chirp_states = temporal_resources[\n                            "chirp_states"\n                        ]\n                        chirp_inst_freq = temporal_resources[\n                            "chirp_inst_freq"\n                        ]\n                        intensity_states = temporal_resources[\n                            "intensity_states"\n                        ]\n                        intensity_levels_pct = temporal_resources[\n                            "intensity_levels_pct"\n                        ]\n\n                        details = {\n                            "temporal_mode":\n                                mode,\n                            "protocol":\n                                "A basal → B ON/OFF → C frequency chirp → "\n                                "D intensity ramp",\n                            "basal_frames":\n                                basal_f,\n                            "step_on_frames":\n                                step_on_f,\n                            "step_off_frames":\n                                step_off_f,\n                            "pause1_frames":\n                                pause1_f,\n                            "chirp_frames":\n                                chirp_duration_f,\n                            "frequency_min_hz":\n                                f_min,\n                            "frequency_max_hz":\n                                f_max,\n                            "frequency_progression":\n                                "linear",\n                            "frequency_duty_cycle":\n                                0.5,\n                            "pause2_frames":\n                                pause2_f,\n                            "intensity_frames":\n                                intensity_duration_f,\n                            "intensity_frequency_hz":\n                                intensity_freq,\n                            "intensity_start_pct":\n                                intensity_start_pct,\n                            "intensity_end_pct":\n                                100.0,\n                            "intensity_effective_cycles":\n                                (\n                                    max(\n                                        1,\n                                        int(\n                                            math.floor(\n                                                (\n                                                    max(\n                                                        0,\n                                                        intensity_duration_f\n                                                        - 1\n                                                    )\n                                                    / hz\n                                                )\n                                                * intensity_freq\n                                            )\n                                        )\n                                        + 1\n                                    )\n                                    if intensity_duration_f > 0\n                                    else 0\n                                ),\n                            "intensity_last_on_level_pct":\n                                (\n                                    max(\n                                        intensity_levels_pct\n                                    )\n                                    if intensity_levels_pct\n                                    else None\n                                ),\n                            "intensity_duty_cycle":\n                                0.5,\n                            "on_color_rgb_255":\n                                cfg[\n                                    "chirp_on_color_rgb"\n                                ],\n                            "off_color_rgb_255":\n                                cfg[\n                                    "chirp_off_color_rgb"\n                                ],\n                            "final_gray_frames":\n                                final_gray_f,\n                            "repetitions":\n                                reps,\n                            "ttl_principal_conditions":\n                                [\n                                    "B",\n                                    "C",\n                                    "D"\n                                ],\n                        }\n\n                        for rep in range(\n                            1,\n                            reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                basal_f,\n                                "chirp_intensity",\n                                rep,\n                                "A_BASAL",\n                                "A_gray"\n                            )\n\n                            if (\n                                step_on_f\n                                + step_off_f\n                            ) > 0:\n                                queue_ttl_event(\n                                    "B_START",\n                                    rep,\n                                    "B_ON_OFF"\n                                )\n\n                            solid_frames(\n                                tuple(\n                                    high_color.tolist()\n                                ),\n                                step_on_f,\n                                "chirp_intensity",\n                                rep,\n                                "B_ON",\n                                "B_step_on"\n                            )\n                            solid_frames(\n                                tuple(\n                                    low_color.tolist()\n                                ),\n                                step_off_f,\n                                "chirp_intensity",\n                                rep,\n                                "B_OFF",\n                                "B_step_off"\n                            )\n\n                            solid_frames(\n                                GRAY,\n                                pause1_f,\n                                "chirp_intensity",\n                                rep,\n                                "PAUSE_B_C",\n                                "gray_pause_1"\n                            )\n\n                            if chirp_duration_f > 0:\n                                queue_ttl_event(\n                                    "C_START",\n                                    rep,\n                                    "FREQUENCY_CHIRP"\n                                )\n\n                            for frame_idx in range(\n                                chirp_duration_f\n                            ):\n                                check_abort()\n\n                                if chirp_states[\n                                    frame_idx\n                                ]:\n                                    color = tuple(\n                                        high_color.tolist()\n                                    )\n                                    state_name = (\n                                        "ON"\n                                    )\n                                else:\n                                    color = tuple(\n                                        low_color.tolist()\n                                    )\n                                    state_name = (\n                                        "OFF"\n                                    )\n\n                                fullfield.fillColor = (\n                                    color\n                                )\n                                fullfield.lineColor = (\n                                    color\n                                )\n                                fullfield.draw()\n\n                                flip_and_log(\n                                    "chirp_intensity",\n                                    rep,\n                                    (\n                                        f"C_CHIRP_{state_name}_"\n                                        f"{chirp_inst_freq[frame_idx]:.4f}Hz"\n                                    ),\n                                    "C_frequency_chirp",\n                                    frame_idx + 1\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                pause2_f,\n                                "chirp_intensity",\n                                rep,\n                                "PAUSE_C_D",\n                                "gray_pause_2"\n                            )\n\n                            if intensity_duration_f > 0:\n                                queue_ttl_event(\n                                    "D_START",\n                                    rep,\n                                    "INTENSITY_RAMP"\n                                )\n\n                            for frame_idx in range(\n                                intensity_duration_f\n                            ):\n                                check_abort()\n\n                                if intensity_states[\n                                    frame_idx\n                                ]:\n                                    pct = (\n                                        intensity_levels_pct[\n                                            frame_idx\n                                        ]\n                                    )\n                                    alpha = (\n                                        pct\n                                        / 100.0\n                                    )\n                                    color_vec = (\n                                        low_color\n                                        + alpha\n                                        * (\n                                            high_color\n                                            - low_color\n                                        )\n                                    )\n                                    condition = (\n                                        f"D_ON_{pct:.3f}%"\n                                    )\n                                else:\n                                    color_vec = (\n                                        low_color\n                                    )\n                                    condition = (\n                                        "D_OFF"\n                                    )\n\n                                color = tuple(\n                                    color_vec.tolist()\n                                )\n                                fullfield.fillColor = (\n                                    color\n                                )\n                                fullfield.lineColor = (\n                                    color\n                                )\n                                fullfield.draw()\n\n                                flip_and_log(\n                                    "chirp_intensity",\n                                    rep,\n                                    condition,\n                                    "D_intensity_ramp",\n                                    frame_idx + 1\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                final_gray_f,\n                                "chirp_intensity",\n                                rep,\n                                "FINAL_GRAY",\n                                "final_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"Temporal · Chirp + Intensity · "\n                                        f"rep {rep}/{reps}",\n                                    "hz":\n                                        hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    elif mode == "Sinusoidal Flicker":\n                        duration_f = seconds_to_frames(\n                            cfg[\n                                "temporal_duration_s"\n                            ],\n                            hz\n                        )\n                        contrast_fraction = float(\n                            temporal_resources[\n                                "contrast_fraction"\n                            ]\n                        )\n                        sine_values = temporal_resources[\n                            "sine_values"\n                        ]\n                        frequency = float(\n                            cfg[\n                                "temporal_frequency_hz"\n                            ]\n                        )\n\n                        details = {\n                            "temporal_mode":\n                                mode,\n                            "basal_frames":\n                                basal_f,\n                            "stimulus_frames":\n                                duration_f,\n                            "frequency_hz":\n                                frequency,\n                            "contrast_pct":\n                                float(\n                                    cfg[\n                                        "temporal_contrast_pct"\n                                    ]\n                                ),\n                            "phase_start_cycles":\n                                0.0,\n                            "modulation":\n                                "sinusoidal about color midpoint",\n                            "high_color_rgb_255":\n                                cfg[\n                                    "chirp_on_color_rgb"\n                                ],\n                            "low_color_rgb_255":\n                                cfg[\n                                    "chirp_off_color_rgb"\n                                ],\n                            "final_gray_frames":\n                                final_gray_f,\n                            "repetitions":\n                                reps,\n                            "ttl_principal_conditions":\n                                [\n                                    "SINUSOIDAL_FLICKER"\n                                ],\n                        }\n\n                        for rep in range(\n                            1,\n                            reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                basal_f,\n                                "chirp_intensity",\n                                rep,\n                                "TEMPORAL_BASAL",\n                                "baseline_gray"\n                            )\n\n                            if duration_f > 0:\n                                queue_ttl_event(\n                                    "SINE_FLICKER_START",\n                                    rep,\n                                    "SINUSOIDAL_FLICKER"\n                                )\n\n                            for frame_idx in range(\n                                duration_f\n                            ):\n                                check_abort()\n\n                                modulation = (\n                                    contrast_fraction\n                                    * float(\n                                        sine_values[\n                                            frame_idx\n                                        ]\n                                    )\n                                )\n                                color_vec = (\n                                    midpoint_color\n                                    + half_range\n                                    * modulation\n                                )\n                                color = tuple(\n                                    color_vec.tolist()\n                                )\n\n                                fullfield.fillColor = (\n                                    color\n                                )\n                                fullfield.lineColor = (\n                                    color\n                                )\n                                fullfield.draw()\n\n                                flip_and_log(\n                                    "chirp_intensity",\n                                    rep,\n                                    "SINUSOIDAL_FLICKER",\n                                    "sinusoidal_flicker",\n                                    frame_idx + 1\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                final_gray_f,\n                                "chirp_intensity",\n                                rep,\n                                "FINAL_GRAY",\n                                "final_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"Temporal · Sinusoidal · "\n                                        f"rep {rep}/{reps}",\n                                    "hz":\n                                        hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    elif mode == "Gaussian Flicker":\n                        duration_f = seconds_to_frames(\n                            cfg[\n                                "temporal_duration_s"\n                            ],\n                            hz\n                        )\n                        frames_per_update = int(\n                            temporal_resources[\n                                "frames_per_update"\n                            ]\n                        )\n                        actual_update_hz = float(\n                            temporal_resources[\n                                "actual_update_hz"\n                            ]\n                        )\n                        sequences = temporal_resources[\n                            "gaussian_sequences"\n                        ]\n                        used_seeds = temporal_resources[\n                            "used_seeds"\n                        ]\n                        contrast_fraction = float(\n                            temporal_resources[\n                                "contrast_fraction"\n                            ]\n                        )\n                        requested_update_hz = float(\n                            cfg[\n                                "temporal_update_hz"\n                            ]\n                        )\n\n                        details = {\n                            "temporal_mode":\n                                mode,\n                            "basal_frames":\n                                basal_f,\n                            "stimulus_frames":\n                                duration_f,\n                            "requested_update_hz":\n                                requested_update_hz,\n                            "actual_update_hz":\n                                actual_update_hz,\n                            "update_limited_by_refresh":\n                                bool(\n                                    requested_update_hz\n                                    > hz\n                                ),\n                            "frames_per_update":\n                                frames_per_update,\n                            "n_updates":\n                                int(\n                                    temporal_resources[\n                                        "n_updates"\n                                    ]\n                                ),\n                            "contrast_pct":\n                                float(\n                                    cfg[\n                                        "temporal_contrast_pct"\n                                    ]\n                                ),\n                            "gaussian_mean":\n                                "color_midpoint",\n                            "gaussian_sigma_normalized":\n                                (\n                                    1.0\n                                    / 3.0\n                                ),\n                            "gaussian_clip_normalized":\n                                [\n                                    -1.0,\n                                    1.0\n                                ],\n                            "seed_base":\n                                int(\n                                    cfg[\n                                        "temporal_seed"\n                                    ]\n                                ),\n                            "used_seeds":\n                                used_seeds,\n                            "high_color_rgb_255":\n                                cfg[\n                                    "chirp_on_color_rgb"\n                                ],\n                            "low_color_rgb_255":\n                                cfg[\n                                    "chirp_off_color_rgb"\n                                ],\n                            "final_gray_frames":\n                                final_gray_f,\n                            "repetitions":\n                                reps,\n                            "ttl_principal_conditions":\n                                [\n                                    "GAUSSIAN_FLICKER"\n                                ],\n                        }\n\n                        for rep in range(\n                            1,\n                            reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                basal_f,\n                                "chirp_intensity",\n                                rep,\n                                "TEMPORAL_BASAL",\n                                "baseline_gray"\n                            )\n\n                            if duration_f > 0:\n                                queue_ttl_event(\n                                    "GAUSSIAN_FLICKER_START",\n                                    rep,\n                                    "GAUSSIAN_FLICKER"\n                                )\n\n                            seq = sequences[\n                                rep - 1\n                            ]\n\n                            for frame_idx in range(\n                                duration_f\n                            ):\n                                check_abort()\n\n                                update_idx = min(\n                                    len(seq) - 1,\n                                    frame_idx\n                                    // frames_per_update\n                                )\n                                normalized = (\n                                    contrast_fraction\n                                    * float(\n                                        seq[\n                                            update_idx\n                                        ]\n                                    )\n                                )\n                                color_vec = (\n                                    midpoint_color\n                                    + half_range\n                                    * normalized\n                                )\n                                color = tuple(\n                                    color_vec.tolist()\n                                )\n\n                                fullfield.fillColor = (\n                                    color\n                                )\n                                fullfield.lineColor = (\n                                    color\n                                )\n                                fullfield.draw()\n\n                                flip_and_log(\n                                    "chirp_intensity",\n                                    rep,\n                                    (\n                                        f"GAUSSIAN_"\n                                        f"{normalized:+.6f}"\n                                    ),\n                                    "gaussian_flicker",\n                                    frame_idx + 1\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                final_gray_f,\n                                "chirp_intensity",\n                                rep,\n                                "FINAL_GRAY",\n                                "final_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"Temporal · Gaussian · "\n                                        f"rep {rep}/{reps}",\n                                    "hz":\n                                        hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    elif mode == "Contrast Series":\n                        duration_f = seconds_to_frames(\n                            cfg[\n                                "temporal_duration_s"\n                            ],\n                            hz\n                        )\n                        isi_f = seconds_to_frames(\n                            cfg[\n                                "temporal_contrast_isi_s"\n                            ],\n                            hz\n                        )\n                        levels = temporal_resources[\n                            "contrast_levels_pct"\n                        ]\n                        sine_values = temporal_resources[\n                            "sine_values"\n                        ]\n                        frequency = float(\n                            cfg[\n                                "temporal_frequency_hz"\n                            ]\n                        )\n\n                        details = {\n                            "temporal_mode":\n                                mode,\n                            "basal_frames":\n                                basal_f,\n                            "stimulus_frames_per_contrast":\n                                duration_f,\n                            "gray_between_contrasts_frames":\n                                isi_f,\n                            "frequency_hz":\n                                frequency,\n                            "contrast_levels_pct":\n                                levels,\n                            "phase_start_cycles":\n                                0.0,\n                            "modulation":\n                                "sinusoidal about color midpoint",\n                            "high_color_rgb_255":\n                                cfg[\n                                    "chirp_on_color_rgb"\n                                ],\n                            "low_color_rgb_255":\n                                cfg[\n                                    "chirp_off_color_rgb"\n                                ],\n                            "final_gray_frames":\n                                final_gray_f,\n                            "repetitions":\n                                reps,\n                            "ttl_principal_conditions":\n                                [\n                                    "CONTRAST_LEVEL_ONSET"\n                                ],\n                        }\n\n                        trial = 0\n\n                        for rep in range(\n                            1,\n                            reps + 1\n                        ):\n                            solid_frames(\n                                GRAY,\n                                basal_f,\n                                "chirp_intensity",\n                                rep,\n                                "TEMPORAL_BASAL",\n                                "baseline_gray"\n                            )\n\n                            for level in levels:\n                                trial += 1\n                                condition = (\n                                    f"CONTRAST_{level:g}%"\n                                )\n                                contrast_fraction = (\n                                    float(level)\n                                    / 100.0\n                                )\n\n                                if duration_f > 0:\n                                    queue_ttl_event(\n                                        "CONTRAST_LEVEL_START",\n                                        trial,\n                                        condition\n                                    )\n\n                                for frame_idx in range(\n                                    duration_f\n                                ):\n                                    check_abort()\n\n                                    modulation = (\n                                        contrast_fraction\n                                        * float(\n                                            sine_values[\n                                                frame_idx\n                                            ]\n                                        )\n                                    )\n                                    color_vec = (\n                                        midpoint_color\n                                        + half_range\n                                        * modulation\n                                    )\n                                    color = tuple(\n                                        color_vec.tolist()\n                                    )\n\n                                    fullfield.fillColor = (\n                                        color\n                                    )\n                                    fullfield.lineColor = (\n                                        color\n                                    )\n                                    fullfield.draw()\n\n                                    flip_and_log(\n                                        "chirp_intensity",\n                                        trial,\n                                        condition,\n                                        "contrast_series",\n                                        frame_idx + 1\n                                    )\n\n                                solid_frames(\n                                    GRAY,\n                                    isi_f,\n                                    "chirp_intensity",\n                                    trial,\n                                    condition,\n                                    "contrast_inter_trial_gray"\n                                )\n\n                            solid_frames(\n                                GRAY,\n                                final_gray_f,\n                                "chirp_intensity",\n                                rep,\n                                "FINAL_GRAY",\n                                "final_gray"\n                            )\n\n                            write_json_atomic(\n                                status_path,\n                                {\n                                    "state":\n                                        "running",\n                                    "message":\n                                        f"Temporal · Contrast Series · "\n                                        f"rep {rep}/{reps}",\n                                    "hz":\n                                        hz,\n                                    "command_id":\n                                        command_id,\n                                    "details":\n                                        details\n                                }\n                            )\n\n                    else:\n                        raise ValueError(\n                            f"Modo Temporal desconocido: {mode}"\n                        )\n\n                else:\n                    raise ValueError(\n                        f"Tipo de estímulo desconocido: "\n                        f"{stimulus_type}"\n                    )\n\n\n            if stimulus_type == "protocol":\n                protocol_cfg = cfg\n                protocol_name = str(\n                    protocol_cfg.get(\n                        "protocol_name",\n                        "Protocolo"\n                    )\n                )\n                protocol_version = int(\n                    protocol_cfg.get(\n                        "protocol_version", 1\n                    )\n                )\n                protocol_repetitions = int(\n                    protocol_cfg.get(\n                        "protocol_repetitions", 1\n                    )\n                )\n                protocol_steps = list(\n                    protocol_cfg.get(\n                        "protocol_steps", []\n                    )\n                )\n\n                if protocol_repetitions < 1:\n                    raise ValueError(\n                        "El protocolo debe repetirse al menos una vez."\n                    )\n                if not protocol_steps:\n                    raise ValueError(\n                        "El protocolo no contiene pasos."\n                    )\n\n                protocol_step_results = []\n                protocol_pause_events = []\n                protocol_name_context = protocol_name\n                protocol_version_context = protocol_version\n\n                # ------------------------------------------------------\n                # PRELOAD / PREWARM PHASE\n                # ------------------------------------------------------\n                # Nothing is logged yet. Every warm-up flip is covered by\n                # neutral gray, so timing starts only after all heavy visual\n                # resources have already been created.\n                write_json_atomic(\n                    status_path,\n                    {\n                        "state": "running",\n                        "message": (\n                            f"Preparando protocolo "\n                            f"{protocol_name}..."\n                        ),\n                        "hz": hz,\n                        "command_id": command_id,\n                        "protocol_name":\n                            protocol_name,\n                        "protocol_version":\n                            protocol_version,\n                        "preparing": True,\n                    }\n                )\n\n                protocol_prepared_steps = {}\n\n                for prep_position, prep_step in enumerate(\n                    protocol_steps,\n                    start=1\n                ):\n                    check_abort()\n\n                    if prep_step.get(\n                        "kind"\n                    ) == "stimulus":\n                        write_json_atomic(\n                            status_path,\n                            {\n                                "state": "running",\n                                "message": (\n                                    f"Preparando protocolo · "\n                                    f"paso {prep_position}/"\n                                    f"{len(protocol_steps)} · "\n                                    f"{prep_step.get(\'label\', \'Estímulo\')}"\n                                ),\n                                "hz": hz,\n                                "command_id":\n                                    command_id,\n                                "protocol_name":\n                                    protocol_name,\n                                "protocol_version":\n                                    protocol_version,\n                                "preparing": True,\n                                "preparing_step":\n                                    prep_position,\n                            }\n                        )\n\n                        protocol_prepared_steps[\n                            prep_position\n                        ] = prepare_protocol_step(\n                            prep_step\n                        )\n\n                # Two final gray flips settle the back/front buffers after\n                # any shader/texture warm-up. These are deliberately outside\n                # the experimental log.\n                show_solid(GRAY)\n                show_solid(GRAY)\n\n                # The first logged protocol frame establishes its own timing\n                # reference; no preparation flip can be counted as a drop.\n                previous_flip = None\n\n                for protocol_rep in range(\n                    1,\n                    protocol_repetitions + 1\n                ):\n                    protocol_rep_context = (\n                        protocol_rep\n                    )\n\n                    for step_position, step in enumerate(\n                        protocol_steps,\n                        start=1\n                    ):\n                        check_abort()\n\n                        next_step_name = str(\n                            step.get(\n                                "label",\n                                step.get(\n                                    "stimulus_type",\n                                    step.get(\n                                        "kind",\n                                        "Paso"\n                                    )\n                                )\n                            )\n                        )\n\n                        wait_for_protocol_resume(\n                            protocol_name,\n                            protocol_version,\n                            protocol_rep,\n                            step_position,\n                            len(protocol_steps),\n                            next_step_name,\n                            protocol_pause_events\n                        )\n\n                        check_abort()\n\n                        protocol_step_index_context = int(\n                            step.get(\n                                "step_index",\n                                step_position\n                            )\n                        )\n                        protocol_step_name_context = str(\n                            step.get(\n                                "label",\n                                step.get(\n                                    "stimulus_type",\n                                    step.get(\n                                        "kind",\n                                        "Paso"\n                                    )\n                                )\n                            )\n                        )\n                        protocol_step_kind_context = str(\n                            step.get(\n                                "kind",\n                                "stimulus"\n                            )\n                        )\n\n                        write_json_atomic(\n                            status_path,\n                            {\n                                "state": "running",\n                                "message": (\n                                    f"Protocolo {protocol_name} · "\n                                    f"rep {protocol_rep}/"\n                                    f"{protocol_repetitions} · "\n                                    f"paso {step_position}/"\n                                    f"{len(protocol_steps)} · "\n                                    f"{protocol_step_name_context}"\n                                ),\n                                "hz": hz,\n                                "command_id": command_id,\n                                "protocol_name":\n                                    protocol_name,\n                                "protocol_version":\n                                    protocol_version,\n                                "protocol_rep":\n                                    protocol_rep,\n                                "protocol_step":\n                                    step_position,\n                                "protocol_total_steps":\n                                    len(protocol_steps),\n                            }\n                        )\n\n                        if (\n                            protocol_step_kind_context\n                            == "pause"\n                        ):\n                            prepared_step = None\n                            pause_s = float(\n                                step.get(\n                                    "duration_s", 0.0\n                                )\n                            )\n                            if pause_s <= 0:\n                                raise ValueError(\n                                    "Una pausa del protocolo "\n                                    "tiene duración <= 0."\n                                )\n\n                            pause_f = seconds_to_frames(\n                                pause_s, hz\n                            )\n\n                            pause_color_name = str(step.get("color", "adaptation")).strip().lower()\n                            if pause_color_name == "black":\n                                pause_color = BLACK\n                                pause_label = "negro"\n                            elif pause_color_name == "gray50":\n                                pause_color = (0.0, 0.0, 0.0)\n                                pause_label = "gris medio"\n                            else:\n                                pause_color = GRAY\n                                pause_label = "fondo de adaptación"\n\n                            solid_frames(\n                                pause_color,\n                                pause_f,\n                                "protocol_pause",\n                                protocol_step_index_context,\n                                f"Pausa {pause_label} {pause_s:g} s",\n                                "protocol_pause",\n                                after_first_frame=(\n                                    emit_generic_pause_marker\n                                    if ttl_mode == "generic_ftdi" else None\n                                )\n                            )\n\n                            protocol_step_results.append({\n                                "protocol_rep":\n                                    protocol_rep,\n                                "step_index":\n                                    protocol_step_index_context,\n                                "step_name":\n                                    protocol_step_name_context,\n                                "kind": "pause",\n                                "duration_s":\n                                    pause_s,\n                                "frames":\n                                    pause_f,\n                                "aux_pause_marker":\n                                    bool(ttl_mode == "generic_ftdi"),\n                                "aux_pause_marker_width_ms":\n                                    (\n                                        1000.0 * pause_marker_width_s\n                                        if ttl_mode == "generic_ftdi" else 0.0\n                                    ),\n                            })\n\n                        elif (\n                            protocol_step_kind_context\n                            == "stimulus"\n                        ):\n                            step_cfg = dict(\n                                step.get(\n                                    "config", {}\n                                )\n                            )\n\n                            step_stimulus_type = str(\n                                step_cfg.get(\n                                    "stimulus_type",\n                                    step.get(\n                                        "stimulus_type",\n                                        ""\n                                    )\n                                )\n                            )\n\n                            if not step_stimulus_type:\n                                raise ValueError(\n                                    "Un paso de estímulo no "\n                                    "contiene stimulus_type."\n                                )\n\n                            cfg = step_cfg\n                            stimulus_type = (\n                                step_stimulus_type\n                            )\n                            prepared_step = (\n                                protocol_prepared_steps.get(\n                                    step_position\n                                )\n                            )\n                            details = {}\n\n                            frame_before = global_frame\n                            ttl_before = len(\n                                ttl_events\n                            )\n\n                            run_current_stimulus()\n                            # Each protocol stimulus ends with a clean AUX baseline.\n                            # UNIVERSAL receives no extra EVENT pulse for this reset.\n                            force_generic_aux_low(\n                                f"PROTOCOL_STEP_{protocol_step_index_context}_END_RESET"\n                            )\n\n                            protocol_step_results.append({\n                                "protocol_rep":\n                                    protocol_rep,\n                                "step_index":\n                                    protocol_step_index_context,\n                                "step_name":\n                                    protocol_step_name_context,\n                                "kind": "stimulus",\n                                "stimulus_type":\n                                    step_stimulus_type,\n                                "frames":\n                                    global_frame\n                                    - frame_before,\n                                "ttl_transitions":\n                                    len(ttl_events)\n                                    - ttl_before,\n                                "details":\n                                    json.loads(\n                                        json.dumps(\n                                            details\n                                        )\n                                    ),\n                            })\n\n                        else:\n                            raise ValueError(\n                                "Tipo de paso de protocolo "\n                                f"desconocido: "\n                                f"{protocol_step_kind_context}"\n                            )\n\n                # Release protocol-only references before final result/log.\n                prepared_step = None\n                protocol_prepared_steps.clear()\n\n                # Restore the command-level identity for the single result/log.\n                cfg = protocol_cfg\n                stimulus_type = "protocol"\n\n                details = {\n                    "protocol_id":\n                        protocol_cfg.get(\n                            "protocol_id"\n                        ),\n                    "protocol_name":\n                        protocol_name,\n                    "protocol_version":\n                        protocol_version,\n                    "protocol_repetitions":\n                        protocol_repetitions,\n                    "protocol_steps":\n                        len(protocol_steps),\n                    "preload_before_timing":\n                        True,\n                    "safe_pause_mode":\n                        "between_protocol_steps",\n                    "manual_pause_count":\n                        len(\n                            protocol_pause_events\n                        ),\n                    "manual_pause_total_s":\n                        sum(\n                            float(\n                                pause_item.get(\n                                    "duration_s",\n                                    0.0\n                                )\n                            )\n                            for pause_item\n                            in protocol_pause_events\n                        ),\n                    "manual_pause_events":\n                        protocol_pause_events,\n                    "step_results":\n                        protocol_step_results,\n                }\n\n            else:\n                run_current_stimulus()\n\n            # Always finish in neutral gray. Event pulses normally\n            # returned LOW one frame after onset; if a falling edge is\n            # pending on the final safety flip it is executed and stamped.\n            finish_trigger_run("STIMULUS_END_SAFETY")\n\n            stamp = run_stamp\n            log_prefix = safe_run_prefix\n\n            csv_path = run_dir / (\n                f"{run_base_name}.csv"\n            )\n\n            config_snapshot_path = run_dir / (\n                f"{run_base_name}_config.json"\n            )\n\n            fieldnames = [\n                "stimulus_type", "trial", "condition",\n                "stage", "frame_in_stage", "global_frame",\n                "flip_time_s",\n                "interval_from_previous_flip_s",\n                "expected_interval_s", "interval_error_ms",\n                "possible_dropped_frame", "ttl_state",\n                "protocol_name", "protocol_version",\n                "protocol_rep", "protocol_step_index",\n                "protocol_step_name", "protocol_step_kind"\n            ]\n\n            with csv_path.open(\n                "w", newline="", encoding="utf-8"\n            ) as fh:\n                writer = csv.DictWriter(\n                    fh, fieldnames=fieldnames\n                )\n                writer.writeheader()\n                writer.writerows(rows)\n\n            ttl_csv_path = run_dir / (\n                f"{run_base_name}_ttl.csv"\n            )\n            ttl_fieldnames = [\n                "event_index", "state", "level",\n                "event_name", "trial", "condition",\n                "scheduled_frame", "callback_time_s",\n                "flip_time_s", "mode", "pin",\n                "port", "signal", "maxone_bit",\n                "event_role", "pulse_width_frames",\n                "event_offset_requested_ms",\n                "pulse_width_requested_ms",\n                "frame_anchor_perf_s", "transition_perf_s",\n                "event_offset_measured_ms",\n                "pulse_width_measured_ms", "universal_code",\n                "aux_event_state", "aux_event_level",\n                "aux_event_policy", "universal_pulse_phase",\n                "protocol_name", "protocol_version",\n                "protocol_rep", "protocol_step_index",\n                "protocol_step_name", "protocol_step_kind"\n            ]\n            with ttl_csv_path.open(\n                "w", newline="", encoding="utf-8"\n            ) as ttl_fh:\n                ttl_writer = csv.DictWriter(\n                    ttl_fh, fieldnames=ttl_fieldnames\n                )\n                ttl_writer.writeheader()\n                ttl_writer.writerows(ttl_events)\n\n\n            # ------------------------------------------------------\n            # EXPERIMENT CONFIGURATION SNAPSHOT\n            # ------------------------------------------------------\n            # Primary CSV logs are already safely written at this point.\n            expected_interval_s = (\n                1.0 / hz\n                if hz and hz > 0\n                else None\n            )\n\n            snapshot = {\n                "snapshot_schema_version": 1,\n                "created_at":\n                    datetime.now().isoformat(\n                        timespec="milliseconds"\n                    ),\n                "app_version": cfg.get(\n                    "app_version", ""\n                ),\n                "command_id": command_id,\n                "stimulus_type": stimulus_type,\n                "measured_refresh_hz": hz,\n                "expected_frame_interval_s":\n                    expected_interval_s,\n                "refresh_measurement_method":\n                    "perf_counter_median_of_real_win_flip_intervals",\n                "refresh_measurement_samples_raw":\n                    refresh_measurement.get("n_raw", ""),\n                "refresh_measurement_samples_used":\n                    refresh_measurement.get("n_used", ""),\n                "refresh_measurement_p05_p95_spread_ms":\n                    refresh_measurement.get("p05_p95_spread_ms", ""),\n                "timing_strategy":\n                    "monotonic_real_flip_calibration_plus_absolute_pacing",\n                "screen_index": cfg.get(\n                    "screen_index"\n                ),\n                "screen_width_px": cfg.get(\n                    "screen_width_px"\n                ),\n                "screen_height_px": cfg.get(\n                    "screen_height_px"\n                ),\n                "screen_x": cfg.get(\n                    "screen_x"\n                ),\n                "screen_y": cfg.get(\n                    "screen_y"\n                ),\n                "physical_width_cm": cfg.get(\n                    "physical_width_cm"\n                ),\n                "physical_height_cm": cfg.get(\n                    "physical_height_cm"\n                ),\n                "distance_cm": cfg.get(\n                    "distance_cm"\n                ),\n                "visual_width_deg": width_deg,\n                "visual_height_deg": height_deg,\n                "ttl_mode": cfg.get(\n                    "ttl_mode"\n                ),\n                "ttl_pin": cfg.get(\n                    "ttl_pin"\n                ),\n                "ttl_strategy":\n                    (\n                        "maxone_ftdi_frame_plus_event_pulses"\n                        if ttl_mode == "maxone_ftdi"\n                        else (\n                            "generic_ftdi_universal_pulse_plus_aux_state"\n                            if ttl_mode == "generic_ftdi"\n                            else "principal_condition_event_pulses"\n                        )\n                    ),\n                "trigger_port_requested":\n                    ttl_port_requested,\n                "trigger_port_resolved":\n                    ttl_port_resolved,\n                "frame_sync_signal":\n                    (\n                        "RTS / MaxOne bit 3"\n                        if ttl_mode == "maxone_ftdi"\n                        else ("RTS# -> FRAME_SYNC + EVENT on UNIVERSAL" if ttl_mode == "generic_ftdi" else "")\n                    ),\n                "event_signal":\n                    (\n                        "TX/BREAK / MaxOne bit 0"\n                        if ttl_mode == "maxone_ftdi"\n                        else ("TX/BREAK -> AUX condition state; UNIVERSAL EVENT pulse remains on RTS" if ttl_mode == "generic_ftdi" else "")\n                    ),\n                "frame_sync_pulses":\n                    frame_sync_pulses,\n                "frame_sync_min_width_ms": 1000.0 * (\n                    GenericTTLFTDI.FRAME_PULSE_MIN_S\n                    if ttl_mode == "generic_ftdi"\n                    else MaxOneFTDI.FRAME_PULSE_MIN_S\n                ),\n                "event_offset_requested_ms": (\n                    1000.0 * event_offset_requested_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "event_pulse_min_width_ms": (\n                    1000.0 * event_pulse_width_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "pause_marker_aux_width_ms": (\n                    1000.0 * pause_marker_width_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "universal_encoding": (\n                    "FRAME pulse + EVENT pulse ~3 ms later"\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "aux_event_encoding": (\n                    "ON/OFF: ON=HIGH, OFF=LOW; other principal conditions alternate HIGH/LOW; protocol pauses: short AUX HIGH marker then LOW; forced LOW at stimulus end"\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "adaptation_rgb": cfg.get("adaptation_rgb", [128, 128, 128]),\n                "idle_rgb": cfg.get("idle_rgb", [0, 0, 0]),\n                "session_metadata":\n                    cfg.get(\n                        "session_metadata",\n                        {}\n                    ),\n                "executed_config": cfg,\n                "result_details": details,\n            }\n\n            snapshot_ok = True\n            snapshot_error = ""\n\n            try:\n                write_json_atomic(\n                    config_snapshot_path,\n                    snapshot\n                )\n            except Exception as snapshot_exc:\n                snapshot_ok = False\n                snapshot_error = (\n                    f"{type(snapshot_exc).__name__}: "\n                    f"{snapshot_exc}"\n                )\n\n            result = {\n                "ok": True,\n                "command_id": command_id,\n                "stimulus_type": stimulus_type,\n                "measured_hz": hz,\n                "refresh_measurement_method":\n                    "perf_counter_median_of_real_win_flip_intervals",\n                "refresh_measurement_samples_raw":\n                    refresh_measurement.get("n_raw", ""),\n                "refresh_measurement_samples_used":\n                    refresh_measurement.get("n_used", ""),\n                "refresh_measurement_p05_p95_spread_ms":\n                    refresh_measurement.get("p05_p95_spread_ms", ""),\n                "resolution": [width_px, height_px],\n                "visual_field_deg":\n                    [width_deg, height_deg],\n                "details": details,\n                "timing_strategy":\n                    "monotonic_real_flip_calibration_plus_absolute_pacing",\n                "timing_target_hz": hz,\n                "pacing_wait_total_s": pacing_wait_total_s,\n                "pacing_late_frame_count": pacing_late_frame_count,\n                "pacing_max_lateness_ms":\n                    1000.0 * pacing_max_lateness_s,\n                "possible_dropped_frames":\n                    possible_drops,\n                "total_frames_logged": global_frame,\n                "run_folder": str(run_dir),\n                "log_file": str(csv_path),\n                "ttl_mode": ttl_mode,\n                "ttl_pin": ttl_pin,\n                "ttl_port_requested": ttl_port_requested,\n                "ttl_port_resolved": ttl_port_resolved,\n                "ttl_strategy":\n                    (\n                        "maxone_ftdi_frame_plus_event_pulses"\n                        if ttl_mode == "maxone_ftdi"\n                        else (\n                            "generic_ftdi_universal_pulse_plus_aux_state"\n                            if ttl_mode == "generic_ftdi"\n                            else "principal_condition_event_pulses"\n                        )\n                    ),\n                "frame_sync_pulses": frame_sync_pulses,\n                "frame_sync_min_width_ms": 1000.0 * (\n                    GenericTTLFTDI.FRAME_PULSE_MIN_S\n                    if ttl_mode == "generic_ftdi"\n                    else MaxOneFTDI.FRAME_PULSE_MIN_S\n                ),\n                "event_offset_requested_ms": (\n                    1000.0 * event_offset_requested_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "event_pulse_min_width_ms": (\n                    1000.0 * event_pulse_width_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "pause_marker_aux_width_ms": (\n                    1000.0 * pause_marker_width_s\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "universal_encoding": (\n                    "FRAME pulse + EVENT pulse ~3 ms later"\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "aux_event_encoding": (\n                    "ON/OFF: ON=HIGH, OFF=LOW; other principal conditions alternate HIGH/LOW; protocol pauses: short AUX HIGH marker then LOW; forced LOW at stimulus end"\n                    if ttl_mode == "generic_ftdi" else ""\n                ),\n                "frame_sync_signal":\n                    (\n                        "RTS / MaxOne bit 3"\n                        if ttl_mode == "maxone_ftdi"\n                        else ("RTS# -> FRAME_SYNC + EVENT on UNIVERSAL" if ttl_mode == "generic_ftdi" else "")\n                    ),\n                "event_signal":\n                    (\n                        "TX/BREAK / MaxOne bit 0"\n                        if ttl_mode == "maxone_ftdi"\n                        else ("TX/BREAK -> AUX condition state; UNIVERSAL EVENT pulse remains on RTS" if ttl_mode == "generic_ftdi" else "")\n                    ),\n                "ttl_pulse_width_frames": ttl_pulse_width_frames,\n                "ttl_pulse_width_ms_approx":\n                    (\n                        1000.0 * event_pulse_width_s\n                        if ttl_mode == "generic_ftdi"\n                        else (1000.0 / hz) * ttl_pulse_width_frames\n                    ),\n                "ttl_pulses":\n                    sum(\n                        1 for ev in ttl_events\n                        if ev.get("state") == 1\n                    ),\n                "ttl_transitions": len(ttl_events),\n                "ttl_log_file": str(ttl_csv_path),\n                "config_snapshot_file":\n                    (\n                        str(config_snapshot_path)\n                        if snapshot_ok\n                        else ""\n                    ),\n                "config_snapshot_ok":\n                    snapshot_ok,\n                "config_snapshot_error":\n                    snapshot_error,\n                "protocol_name":\n                    (\n                        details.get(\n                            "protocol_name", ""\n                        )\n                        if stimulus_type == "protocol"\n                        else ""\n                    ),\n                "protocol_version":\n                    (\n                        details.get(\n                            "protocol_version", ""\n                        )\n                        if stimulus_type == "protocol"\n                        else ""\n                    ),\n            }\n\n            write_json_atomic(result_path, result)\n            show_solid(IDLE_COLOR)\n            write_json_atomic(status_path, {\n                "state": "ready",\n                "message":\n                    "Pantalla experimental estable · "\n                    "espera segura",\n                "hz": hz,\n                "screen_index": screen_index,\n                "resolution":\n                    [width_px, height_px],\n                "last_command_id": command_id,\n                "last_result_ok": True\n            })\n\n        except KeyboardInterrupt:\n            # Safe abort: TTL LOW and immediate return to gray;\n            # the PsychoPy window remains open.\n            finish_trigger_run("ABORT_LOW", suppress_errors=True)\n\n            try:\n                abort_map.seek(0)\n                abort_map.write(b"\\x00")\n                abort_map.flush()\n            except Exception:\n                pass\n\n            show_solid(IDLE_COLOR)\n            write_json_atomic(result_path, {\n                "ok": False,\n                "aborted": True,\n                "trigger_error": trigger_backend_fault,\n                "ttl_port_resolved": ttl_port_resolved,\n                "frame_sync_pulses": frame_sync_pulses,\n                "command_id": command_id,\n                "stimulus_type":\n                    command_stimulus_type,\n                "protocol_name":\n                    (\n                        protocol_name_context\n                        if command_stimulus_type\n                        == "protocol"\n                        else ""\n                    ),\n                "protocol_version":\n                    (\n                        protocol_version_context\n                        if command_stimulus_type\n                        == "protocol"\n                        else ""\n                    ),\n                "error": "Estímulo abortado."\n            })\n            write_json_atomic(status_path, {\n                "state": "ready",\n                "message":\n                    "Estímulo abortado · espera segura",\n                "hz": hz,\n                "screen_index": screen_index,\n                "last_command_id": command_id\n            })\n\n        except Exception as exc:\n            # Stimulus errors do NOT destroy the PsychoPy window.\n            # Return both trigger outputs to rest even after an I/O/flip error.\n            finish_trigger_run("ERROR_LOW", suppress_errors=True)\n\n            show_solid(IDLE_COLOR)\n            write_json_atomic(result_path, {\n                "ok": False,\n                "command_id": command_id,\n                "stimulus_type":\n                    command_stimulus_type,\n                "protocol_name":\n                    (\n                        protocol_name_context\n                        if command_stimulus_type\n                        == "protocol"\n                        else ""\n                    ),\n                "protocol_version":\n                    (\n                        protocol_version_context\n                        if command_stimulus_type\n                        == "protocol"\n                        else ""\n                    ),\n                "error":\n                    f"{type(exc).__name__}: {exc}",\n                "traceback": traceback.format_exc(),\n                "trigger_error": trigger_backend_fault,\n                "ttl_port_resolved": ttl_port_resolved,\n                "frame_sync_pulses": frame_sync_pulses,\n            })\n            write_json_atomic(status_path, {\n                "state": "ready",\n                "message":\n                    "Error de estímulo · espera segura",\n                "hz": hz,\n                "screen_index": screen_index,\n                "last_command_id": command_id,\n                "last_result_ok": False\n            })\n\n        finally:\n            trigger_stopping = True\n            close_trigger_backend()\n\n        core.wait(0.005)\n\nexcept Exception as exc:\n    # If the PsychoPy process itself fails, the independent safety\n    # window behind it remains gray.\n    try:\n        write_json_atomic(status_path, {\n            "state": "fatal",\n            "message":\n                f"Motor PsychoPy detenido: "\n                f"{type(exc).__name__}: {exc}",\n            "traceback": traceback.format_exc()\n        })\n    except Exception:\n        pass\n\nfinally:\n    try:\n        cleanup = globals().get("close_trigger_backend")\n        if callable(cleanup):\n            cleanup()\n    except Exception:\n        pass\n    try:\n        if abort_map is not None:\n            abort_map.close()\n    except Exception:\n        pass\n    try:\n        if abort_file is not None:\n            abort_file.close()\n    except Exception:\n        pass\n    try:\n        if win is not None:\n            show_solid(IDLE_COLOR)\n            win.close()\n    except Exception:\n        pass'
app = App()
try:
    app.mainloop()
finally:
    try:
        app._restore_global_gamma_safely()
    except Exception:
        pass
