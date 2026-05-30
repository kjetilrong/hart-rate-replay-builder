# HR Replay Builder — Garmin + miCoach zone modes

HR Replay Builder is a Streamlit app for replaying historical GPX/trace-JSON heart-rate workouts as Garmin FIT workouts and ZWO indoor-training files.

Core features:

- Multi-file GPX/JSON upload.
- Date-preserving plan generation with original pause days retained.
- Smoothed HR analysis and generated workout blocks.
- Manual block editing: move boundaries, change zones, split blocks, and merge the next block.
- Garmin FIT export.
- Fenix 3 explicit custom HR range export using the verified `+100` encoding.
- ZWO export using configurable %FTP proxy values per HR zone.
- YAML/JSON/CSV audit files for plan and workout inspection.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Zone models

The sidebar **Zone model** selector controls how uploaded workouts are classified, displayed, edited, and exported.

### Garmin Z1-Z5 mode

`Garmin Z1-Z5` preserves the existing v8 behavior:

- Five zones: `Z1`, `Z2`, `Z3`, `Z4`, `Z5`.
- Sidebar boundaries are old-workout bpm start values for `Z2`, `Z3`, `Z4`, and `Z5`.
- Historical HR samples are classified by those old bpm boundaries.
- FIT export in `Garmin HR Zone 1-5` mode writes Garmin zone numbers.
- FIT export in `Fenix 3 custom HR range (+100 encoding)` mode writes explicit target bpm ranges derived from the same zone boundaries.
- ZWO export maps each Garmin zone to the configured %FTP proxy.

### miCoach Blue/Green/Yellow/Red mode

`miCoach Blue/Green/Yellow/Red` uses the old adidas miCoach four-zone model:

- `Blue` — easy effort, aerobic base, recovery.
- `Green` — medium effort, endurance, calorie burn.
- `Yellow` — hard effort, cardio strength.
- `Red` — maximum effort, power and speed.

Default old-HRmax-212 boundaries are:

- Blue starts: `120 bpm`
- Green starts: `145 bpm`
- Yellow starts: `165 bpm`
- Red starts: `185 bpm`

Anything below the configured Blue start is treated as Blue for simplicity. The block graph uses Blue/Green/Yellow/Red labels and matching colors, and the manual editor's zone dropdown changes to the four miCoach zones.

## HRmax conversion logic

Both zone models use the same conversion pipeline:

```text
old bpm zone boundary → percentage of Old HRmax → current bpm target using Current HRmax → Garmin FIT target
```

Example with Old HRmax `212`, Current HRmax `180`, and old Green range `145–165 bpm`:

```text
145 / 212 = 68.4%
165 / 212 = 77.8%
180 * 68.4% ≈ 123 bpm
180 * 77.8% ≈ 140 bpm
```

The exported current target range is therefore approximately `123–140 bpm`.

## Garmin FIT export modes

The sidebar **Garmin FIT export mode** selector controls how generated blocks become Garmin workout steps.

### Fenix 3 custom HR range (+100 encoding)

This is the recommended mode for Fenix 3 use. Each block exports as an explicit bpm range rather than depending on the watch profile's configured zones.

The FIT workout step uses:

```text
target_type = heart_rate
target_value = 0
custom_target_value_low = desired_low_bpm + 100
custom_target_value_high = desired_high_bpm + 100
```

For miCoach mode, step names are short, readable uppercase labels such as:

```text
BLUE 102-122
GREEN 123-140
YELLOW 140-156
RED 157-180
```

### Garmin HR Zone 1-5

This is the legacy robust mode. It writes Garmin HR zone numbers as workout step targets. In Garmin Z1-Z5 mode this matches zones `1..5`; in miCoach mode the four colors are mapped to zone numbers `1..4` for compatibility.

## ZWO export

ZWO is power/%FTP based, not HR-target based. HR zones are therefore mapped to power proxy values intended to evoke a similar HR response indoors.

Default miCoach mapping:

- Blue: `0.60 FTP`
- Green: `0.75 FTP`
- Yellow: `0.90 FTP`
- Red: `1.05 FTP`

These defaults are centralized in `MICOACH_ZWO_POWER_DEFAULTS` in `app.py` and are also adjustable in the sidebar.

## Exported workout naming

Exported workout files now use short, Garmin-friendly stems instead of embedding the planned date, original upload filename, or source identifiers:

```text
day_<sequence>_<duration>_<peakzone>_<type>.fit
```

Examples:

```text
day_01_12min_red_test.fit
day_02_25min_red_int.fit
day_03_40min_yellow_tempo.fit
day_04_95min_green_long.fit
day_05_50min_blue_easy.fit
```

ZWO and per-workout YAML audit exports use the same stem, for example `day_01_12min_red_test.zwo` and `day_01_12min_red_test.yaml`. Edited selected-workout exports append `_edited` before the extension, for example `day_01_12min_red_test_edited.fit`.

The stem components are:

- `sequence` — two-digit plan order such as `01` or `02`.
- `duration` — rounded total duration in minutes.
- `peakzone` — the highest zone reached (`blue`/`green`/`yellow`/`red` in miCoach mode, or `z1`–`z5` in Garmin mode).
- `type` — deterministic workout classification: `test`, `long`, `int`, `tempo`, or `easy`. The priority is `test`, then `long`, then `int`, then `tempo`, then `easy`, so a very long hard workout is named `long` unless it qualifies as a short red/Z5 test.

Garmin FIT workout names stay short for Fenix 3 display limits while retaining the rounded duration, such as `D01 12MIN RED TEST`, `D02 25MIN RED INT`, or `D03 45MIN Z4 TEMPO`.

Original source filenames and date traceability are preserved in `plan.csv`, `audit.json`, and each YAML audit file rather than in the exported FIT/ZWO filename. Those audit files include the original source filename, original date, planned date, sequence number, generated short filename, workout type, peak zone, duration, selected zone model, and selected FIT export mode.

## Export workflows

### Single-workout export

The preview download buttons use the selected zone model and selected FIT export mode, but they use the auto-generated blocks for the currently previewed workout.

### Export selected edited workout

After using the manual editor, use **Export selected edited workout** to download FIT/ZWO/YAML from the edited blocks. This is the export path to use when you moved boundaries, changed block zones, split blocks, or merged blocks.

### Export full plan

The full-plan ZIP exports every planned workout using the selected zone model and selected FIT export mode. It uses auto-generated blocks for each workout in the plan.

The ZIP contains:

```text
fit/
zwo/
audit/
plan.csv
audit.json
zone_definitions.json
```

For Garmin, copy each file from `fit/` to:

```text
Garmin/NewFiles
```

Then safely eject/unmount the watch and open the workouts under Training / Workouts / Run.

## Important limitation

Garmin workout FIT files are not calendar-scheduled by the file itself. Planned dates are preserved in `plan.csv`, `audit.json`, and audit YAML; exported workout filenames stay short for easier Garmin/Fenix use.
