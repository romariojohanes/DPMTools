# DPM+ v1.0 managed-folder release checklist

- [ ] `python -m tools.release_check --preflight` confirms Python 3.12 and every direct pinned dependency.
- [ ] `python -m pip check` reports no broken requirements.
- [ ] The full pytest suite passes from unrestricted local PowerShell.
- [ ] Automatic canonical headers and independent Legacy/SaaS custom mappings both complete reconciliation.
- [ ] Invalid, missing, and duplicate mappings fail before background processing starts.
- [ ] Progress, cancellation, validation errors, resource stops, and background errors remain visible and safe.
- [ ] Every Streamlit results page opens without an exception for a completed run.
- [ ] All 13 Excel reports reopen with their required evidence sheets.
- [ ] All nine Parquet details match their source schema and row count.
- [ ] Verified Excel and Parquet files up to 25 MB expose browser download controls; larger files show their local path.
- [ ] `start.bat` starts from the managed folder, binds to localhost, disables telemetry, and reports preflight/start failures clearly.
- [ ] Setup, mapping, usage, recovery, download limits, and managed-folder/non-EXE constraints match observed behavior.
- [ ] Historical benchmark evidence and existing resource/security limits are unchanged.

Record any unresolved issue below. DPM+ v1.0 is NO-GO while a critical or high-severity issue remains.
