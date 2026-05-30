
"""
Minimal Garmin FIT workout writer.

Creates workout FIT files with:
- file_id
- workout
- workout_step messages

Workout steps use Garmin heart-rate zones:
    target_type = heart_rate
    target_value = 1..5

The watch uses the HR zones configured on the Garmin device/profile.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

ENUM = 0x00
UINT8 = 0x02
UINT16 = 0x84
UINT32 = 0x86
UINT32Z = 0x8C
STRING = 0x07

MESG_FILE_ID = 0
MESG_WORKOUT = 26
MESG_WORKOUT_STEP = 27

FILE_TYPE_WORKOUT = 5
MANUFACTURER_DEVELOPMENT = 255
SPORT_RUNNING = 1

DURATION_TIME = 0
TARGET_HEART_RATE = 1
TARGET_OPEN = 2

INTENSITY_ACTIVE = 0
INTENSITY_REST = 1
INTENSITY_WARMUP = 2
INTENSITY_COOLDOWN = 3
INTENSITY_RECOVERY = 4
INTENSITY_INTERVAL = 5

PROFILE_VERSION = 21176


@dataclass
class FitWorkoutStep:
    duration_s: int
    zone: int
    name: str = ""
    intensity: int = INTENSITY_ACTIVE


@dataclass
class FitCustomHrRangeStep:
    """Workout step with explicit HR bpm range.

    For Fenix 3 compatibility, create_fit_workout_custom_hr_ranges()
    stores low/high as bpm + 100 while target_value is set to 0.
    User testing showed this is rendered correctly by Fenix 3.
    """
    duration_s: int
    hr_low: int
    hr_high: int
    name: str = ""
    intensity: int = INTENSITY_ACTIVE


def fit_timestamp(dt: datetime | None = None) -> int:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt.astimezone(timezone.utc) - FIT_EPOCH).total_seconds())


def fit_crc(data: bytes) -> int:
    crc_table = [
        0x0000, 0xCC01, 0xD801, 0x1400,
        0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401,
        0x5000, 0x9C01, 0x8801, 0x4400,
    ]
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]

        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc & 0xFFFF


def padded_string(value: str, size: int) -> bytes:
    raw = (value or "").encode("utf-8")[: max(0, size - 1)]
    return raw + b"\x00" * (size - len(raw))


def definition_record(local_num: int, global_mesg_num: int, fields: list[tuple[int, int, int]]) -> bytes:
    out = bytearray()
    out.append(0x40 | (local_num & 0x0F))
    out.append(0)
    out.append(0)
    out.extend(struct.pack("<H", global_mesg_num))
    out.append(len(fields))
    for field_num, size, base_type in fields:
        out.extend(bytes([field_num, size, base_type]))
    return bytes(out)


def data_record(local_num: int, payload: bytes) -> bytes:
    return bytes([local_num & 0x0F]) + payload


def create_fit_workout(
    name: str,
    steps: Iterable[FitWorkoutStep],
    sport: int = SPORT_RUNNING,
    serial_number: int = 0x12345678,
) -> bytes:
    steps = list(steps)
    workout_name_size = 32
    step_name_size = 16
    notes_size = 32

    data = bytearray()

    data.extend(definition_record(
        0,
        MESG_FILE_ID,
        [
            (0, 1, ENUM),
            (1, 2, UINT16),
            (2, 2, UINT16),
            (3, 4, UINT32Z),
            (4, 4, UINT32),
        ],
    ))
    data.extend(data_record(
        0,
        struct.pack(
            "<BHHII",
            FILE_TYPE_WORKOUT,
            MANUFACTURER_DEVELOPMENT,
            1,
            serial_number,
            fit_timestamp(),
        ),
    ))

    data.extend(definition_record(
        1,
        MESG_WORKOUT,
        [
            (4, 1, ENUM),
            (6, 2, UINT16),
            (8, workout_name_size, STRING),
        ],
    ))
    data.extend(data_record(
        1,
        struct.pack("<BH", sport, len(steps)) + padded_string(name, workout_name_size),
    ))

    data.extend(definition_record(
        2,
        MESG_WORKOUT_STEP,
        [
            (254, 2, UINT16),
            (0, step_name_size, STRING),
            (1, 1, ENUM),
            (2, 4, UINT32),
            (3, 1, ENUM),
            (4, 4, UINT32),
            (7, 1, ENUM),
            (8, notes_size, STRING),
        ],
    ))

    for idx, step in enumerate(steps):
        zone = int(step.zone)
        if zone < 1 or zone > 5:
            zone = 0
            target_type = TARGET_OPEN
        else:
            target_type = TARGET_HEART_RATE

        duration_ms = int(round(step.duration_s * 1000))
        step_name = step.name or f"Z{zone} {step.duration_s}s"
        notes = f"HR Zone {zone}" if zone else "Open"

        payload = (
            struct.pack("<H", idx)
            + padded_string(step_name, step_name_size)
            + struct.pack("<BI B I B", DURATION_TIME, duration_ms, target_type, zone, int(step.intensity))
            + padded_string(notes, notes_size)
        )
        data.extend(data_record(2, payload))

    header_without_crc = struct.pack("<BBHI4s", 14, 0x10, PROFILE_VERSION, len(data), b".FIT")
    header = header_without_crc + struct.pack("<H", fit_crc(header_without_crc))
    file_without_crc = header + bytes(data)
    return file_without_crc + struct.pack("<H", fit_crc(file_without_crc))



def create_fit_workout_custom_hr_ranges(
    name: str,
    steps: Iterable[FitCustomHrRangeStep],
    sport: int = SPORT_RUNNING,
    serial_number: int = 0x1234567B,
    fenix3_offset_100: bool = True,
) -> bytes:
    """Create a Garmin workout FIT file using custom HR bpm ranges.

    Encoding validated by user test on Garmin Fenix 3:
      target_type = heart_rate
      target_value = 0
      custom_target_value_low/high = desired bpm + 100

    If fenix3_offset_100=False, low/high are stored as raw bpm, but the
    default should remain True for this project.
    """
    steps = list(steps)
    workout_name_size = 32
    step_name_size = 16
    notes_size = 32

    data = bytearray()

    data.extend(definition_record(
        0,
        MESG_FILE_ID,
        [
            (0, 1, ENUM),
            (1, 2, UINT16),
            (2, 2, UINT16),
            (3, 4, UINT32Z),
            (4, 4, UINT32),
        ],
    ))
    data.extend(data_record(
        0,
        struct.pack(
            "<BHHII",
            FILE_TYPE_WORKOUT,
            MANUFACTURER_DEVELOPMENT,
            4,
            serial_number,
            fit_timestamp(),
        ),
    ))

    data.extend(definition_record(
        1,
        MESG_WORKOUT,
        [
            (4, 1, ENUM),
            (6, 2, UINT16),
            (8, workout_name_size, STRING),
        ],
    ))
    data.extend(data_record(
        1,
        struct.pack("<BH", sport, len(steps)) + padded_string(name, workout_name_size),
    ))

    # workout_step fields:
    # 254 message_index
    # 0   wkt_step_name
    # 1   duration_type
    # 2   duration_time, scale 1000
    # 3   target_type = heart_rate
    # 4   target_value = 0 for Fenix 3 custom HR range
    # 5   custom_target_value_low
    # 6   custom_target_value_high
    # 7   intensity
    # 8   notes
    data.extend(definition_record(
        2,
        MESG_WORKOUT_STEP,
        [
            (254, 2, UINT16),
            (0, step_name_size, STRING),
            (1, 1, ENUM),
            (2, 4, UINT32),
            (3, 1, ENUM),
            (4, 4, UINT32),
            (5, 4, UINT32),
            (6, 4, UINT32),
            (7, 1, ENUM),
            (8, notes_size, STRING),
        ],
    ))

    for idx, step in enumerate(steps):
        hr_low = max(1, int(round(step.hr_low)))
        hr_high = max(hr_low + 1, int(round(step.hr_high)))

        stored_low = hr_low + 100 if fenix3_offset_100 else hr_low
        stored_high = hr_high + 100 if fenix3_offset_100 else hr_high

        duration_ms = int(round(step.duration_s * 1000))
        step_name = step.name or f"HR {hr_low}-{hr_high}"

        payload = (
            struct.pack("<H", idx)
            + padded_string(step_name, step_name_size)
            + struct.pack(
                "<BI B I I I B",
                DURATION_TIME,
                duration_ms,
                TARGET_HEART_RATE,
                0,  # Fenix 3-friendly custom range marker.
                stored_low,
                stored_high,
                int(step.intensity),
            )
            + padded_string(f"{hr_low}-{hr_high} bpm", notes_size)
        )
        data.extend(data_record(2, payload))

    header_without_crc = struct.pack("<BBHI4s", 14, 0x10, PROFILE_VERSION, len(data), b".FIT")
    header = header_without_crc + struct.pack("<H", fit_crc(header_without_crc))
    file_without_crc = header + bytes(data)
    return file_without_crc + struct.pack("<H", fit_crc(file_without_crc))


def write_fit_workout(path: str, name: str, steps: Iterable[FitWorkoutStep]) -> None:
    with open(path, "wb") as f:
        f.write(create_fit_workout(name=name, steps=steps))
