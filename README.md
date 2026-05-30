# HR Replay Builder v6 — Plan + FIT + ZWO

This version is built on **v5 FIT + ZWO** and adds the missing plan workflow:

- Multi-file upload
- Sort workouts by original date
- Choose a new start date
- Preserve the original day offsets / pause days
- Show original running days vs new planned running days
- Export one Garmin `.fit` per planned workout
- Export one `.zwo` per planned workout
- Export `plan.csv`, `audit.json`, and YAML audit files

## What stayed from v5

- Minimum instruction block down to **15 seconds**
- Garmin FIT export with HR-zone targets
- ZWO export for Rouvy/Zwift-style indoor workouts
- ZWO %FTP proxy per HR zone
- Visual HR graph with instruction blocks

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Garmin import

The exported ZIP contains:

```text
fit/
zwo/
audit/
plan.csv
audit.json
```

For Garmin, copy each file from `fit/` to:

```text
Garmin/NewFiles
```

Then safely eject/unmount the watch and open the workouts under Training / Workouts / Run.

## Important limitation

Garmin workout FIT files are not calendar-scheduled by the file itself.
The planned dates are preserved in `plan.csv` and in the filenames.


## v7 manual block editing

This version adds manual block-boundary editing.

Plotly/Streamlit does not provide reliable direct drag handles for workout spans without a custom frontend component.  
So v7 uses a deterministic editor below the graph:

- select boundary between two blocks
- move it left/right by 15/30/60/120 seconds
- change zone for a selected block
- split a block
- merge with next block
- graph redraws with edited blocks
- selected edited FIT export uses the edited blocks

Typical use case:

```text
Z5 is too long near the end.
Select boundary: Z5 → Z4
Move boundary left by 30s or 60s
Download selected edited Garmin FIT
```

For now, the full plan ZIP still uses auto-generated blocks for all workouts.  
Manual edits are exported via "Download selected edited Garmin FIT".


## v7.1 hotfix

Fixes crash:

```text
NameError: name 'z2' is not defined
```

The manual-edit session key now derives zone settings from `zone_defs` rather than local sidebar variable names.


## v7.2 hotfix

Fixes export crash:

```text
TypeError: blocks_to_yaml() got an unexpected keyword argument 'note'
```

The edited YAML export now calls the existing `blocks_to_yaml()` signature used by the v6/v7 codebase.


## v7.3 hotfix

Fixes export crash:

```text
TypeError: blocks_to_yaml() takes 5 positional arguments but 6 were given
```

The edited YAML export no longer passes the selected workout object into `blocks_to_yaml()`.


## v8 Fenix 3 custom HR range export

This version implements the Fenix 3 custom HR range mode verified by user testing.

Garmin FIT export modes:

1. `Fenix 3 custom HR range (+100 encoding)`  
   Exports explicit bpm ranges per block, using:
   - target_type = heart_rate
   - target_value = 0
   - custom_target_value_low = desired_low_bpm + 100
   - custom_target_value_high = desired_high_bpm + 100

2. `Garmin HR Zone 1-5`  
   Legacy robust export using Garmin zone numbers.

Recommended for this project:
- Use Fenix 3 custom HR range mode.
- Use the manual block editor to adjust durations.
- Export selected edited Garmin FIT after manual edits.
