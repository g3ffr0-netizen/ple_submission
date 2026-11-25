All identified gaps have been resolved and verified on disk in `/filesystem/final_batch4__electrical_task12_conveyance_study__blind/output/submission.zip` (containing `conveyance_study.xlsx` and `conveyance_study_report.md` at the zip root).

---

### 1. Deliverable Paths
- **Submission Archive**: `/filesystem/final_batch4__electrical_task12_conveyance_study__blind/output/submission.zip`
- **Workbook (at zip root)**: `conveyance_study.xlsx` (also saved at `/filesystem/final_batch4__electrical_task12_conveyance_study__blind/output/conveyance_study.xlsx`)
- **Study Report (at zip root)**: `conveyance_study_report.md` (also saved at `/filesystem/final_batch4__electrical_task12_conveyance_study__blind/output/conveyance_study_report.md`)

---

### 2. Summary of Corrections Applied

1. **Design Currents (`design_current_a`) & `C05` Cable Sizing (`Protective_Device_Settings_Schedule_RevD.xlsx` & `TD-15AL-133_Ampacity_Tables.xlsx`)**:
   - Updated `design_current_a` across `Items`, `Register`, and `conveyance_study_report.md` to match the phase pickup (`51P`) settings in `Protective_Device_Settings_Schedule_RevD.xlsx`:
     - **C01 (`F1`, `52-F1`)**: `640.0 A` (`2` sets of `500 kcmil` in `2x2` `6-in` ducts, `331.55 A/set`, total `663.10 A >= 640 A`).
     - **C02 (`F2`, `52-F2`)**: `560.0 A` (`2` sets of `500 kcmil` in `2x2` `6-in` ducts, `331.55 A/set`, total `663.10 A >= 560 A`).
     - **C03 (`F3`, `52-F3`)**: `400.0 A` (`1` set of `350 kcmil` in `1x2` `5-in` ducts, `410.12 A/set >= 400 A`).
     - **C04 (`CF-2`, `52-CF2`)**: **`60.0 A`** (`1` set of `250 kcmil` in `1x2` `5-in` ducts, `330.48 A/set >= 60 A`; `250 kcmil` required for `14.2 kA @ 0.5 s` fault withstand).
     - **C05 (`A-3`, `52-A3`)**: **`200.0 A`** (**`2` sets of `250 kcmil`** in `5-in` `RMC`, `160.00 A/set`, total `320.00 A >= 200 A`, `jam_ratio = 3.631` outside `[2.8, 3.2]`, `fill_pct = 22.76% <= 40%`).
     - **C06 (`F4`, `52-F4`)**: **`40.0 A`** (`OH` with `1/0` ACSR Raven at `196.00 A >= 40 A` and `1` set of `250 kcmil` on underground riser leads for `16.5 kA @ 0.5 s` fault withstand).
     - **C07 (`CF-1`, `52-CF1`)**: **`120.0 A`** (`1` set of `250 kcmil` in `1x2` `5-in` ducts, `330.48 A/set >= 120 A`; `250 kcmil` required for `14.2 kA @ 0.5 s` fault withstand).

2. **Exact Labor Codes, Crew Compositions, and Craft Classifications (`Labor_Unit_Database_PCEC_2026.xlsx`, `Payroll_Burden_and_Insurance_2026.xlsx`, & RFP 3.8)**:
   - Replaced all `Takeoff` labor codes with the exact codes from `Labor_Unit_Database_PCEC_2026.xlsx` and RFP Section 3.8 (`EX-MACH`, `EX-TRCH`, `BF-COMP`, `SAND-BED`, `DUCT-CE`, `DUCT-UE`, `CONC-PL`, `REBAR`, `GND-DB`, `TAPE`, `PAVE-CUT`, `MH-SET`, `MH-BASE`, `PULL-MV-L`, `DBC-LAY`, `TERM-MV`, `FIREPROOF`, `POLE-SET`, `POLE-HOLE`, `XARM`, `GUY`, `STRING`, `RISER`, `RMC-5`, `EMT-4`, `ELBOW-5`, `PBOX`, `TRAY-24`, `TRAY-COV`, `TRAY-BOND`, `PULL-IN`, `FIRE-SLV`, `FIRE-RMC`, `SEISMIC`, `TERM-IN`) using the exact crew compositions (`CR-EX`, `CR-TM`, `CR-BF`, `CR-DU`, `CR-CO`, `CR-CP`, `CR-SP`, `CR-MH`, `CR-DW`, `CR-OH`, `CR-OH2`, `CR-OH3`, `CR-STR`, `CR-IN`, `CR-IS`) and `67/80` and `0.85` multipliers per the database notes.
   - Updated `LaborRates` craft class code to `ELECTRICIAN` (`$60.80` base, `$31.59` fringe, `15.77%` burden, `$101.98/hr` loaded) and `PUMPOP` (`OPERATOR: POWER EQUIPMENT, ALL OTHER WORK: GROUP 1 (PUMP OPERATOR)`, `$63.40` base, `$33.20` fringe, `19.00%` burden, `$108.65/hr` loaded).

3. **Exact Material Codes, Quoted Prices, Waste Allowances, and PPI Index Adjustment (`Supplier_Quote_Q-2608-114.pdf` & `BLS_PPI_WPU102603_2024-2026.json`)**:
   - Applied exact material codes, unit prices, and waste allowances (`SPACER` `$3.10`/0%, `CONC-3000` `$212.00`/8%, `SAND` `$48.00`/10%, `ROCK-BASE` `$62.00`/10%, `MH-PRECAST` `$16,850.00`/0%, `TERM-KIT` `$265.00`/0%, `SPLICE-KIT` `$410.00`/0%, `FIRETAPE` `$3.40`/5%, `POLE-45` `$1,480.00`, `POLE-50` `$1,760.00`, `XARM-ASSY` `$385.00`, `GUY-ASSY` `$420.00`, `RISER-ASSY` `$3,950.00`, `ELBOW-RGS5` `$640.00`, `PBOX-36` `$1,180.00`, `TRAY-24-SB` `$38.00`/5%, `TRAY-24-LD` `$29.00`/5%, `TRAY-COV-24` `$12.50`/5%, `BOND-2` `$2.10`/3% `(I)`, `FIRE-SLV-4` `$310.00`, `FIRESTOP-RMC5` `$145.00`, `SEISMIC-BR` `$165.00`).
   - Applied BLS PPI series `WPU102603` index ratio `494.096 / 482.612 = 1.0237955` (August 2026 vs. July 2026 base) and `9.5%` sales tax, and updated conduit average IDs (`EB20-6` = `6.265 in`, `EMT-4IN` = `4.334 in`).

4. **Exact Subcontract HDD Pricing (`Subcontract_Quote_ADD-26-311_HDD.pdf`), RFP 3.7 Take-Off Conventions, Pulling Calculations (`TD-15AL-133`), and Surety Bond Schedule (`Surety_Bond_Rate_Letter_2026.pdf`)**:
   - Priced each directional bore using `$64.00/LF` (2 conduits) or `$92.00/LF` (4 conduits), `+15%` deep bore surcharge where cover exceeds `96 in` (`DC-1` channel bore at `120 in` cover), plus `$5,400.00` per bore with the `$14,000.00` per-bore minimum charge.
   - Applied all RFP 3.7 conventions (`+3 in` bedding in trench depth, `2(w-3 in)+2(h-3 in)` tie length with `floor(L/3)+1` ties, `ducts * (floor(L/5)+1)` spacers, `7.33 x 9.33 x 8.0 ft` manhole outside volume and `8.0 ft` excavation depth with `0.5 ft` rock base, `12` cond-ft `FIREPROOF`/`FIRETAPE` per conductor per manhole, `100 ft` `PAVE-CUT`/`ASPHALT` across `CUP-YARD` on `C04`, and exact cable circuit-foot and overhead formulas).
   - Recomputed all cable pulls bidirectionally with `100 lb` reel feed-in tension, exact cradled/triangular `wc`, conduit friction (`0.35` PVC, `0.30` HDPE, `0.40` RGS), `90-deg` `3.0-ft` vertical riser elbows at equipment terminals/riser poles, and cradled SWBP `(3*wc - 2)*T / (3*R)`.
   - Applied the 5-tier Class B Surety Bond schedule (`$100k @ $25/k`, `$300k @ $15/k`, `$1,600k @ $10/k`, `$500k @ $7.50/k`, over `$2,500k @ $6.50/k`, `+3%` surcharge for 15 months), yielding `bond_rate_pct = 1.2463%`.

---

### 3. Final Verified Summary of Recommended Conveyances (`Items`)

| Item | Recommended Type | Cable Size (AWG/kcmil) | Sets | OH Conductor (ACSR) | Design Current (A) | Ampacity / Set (A) | Route Length (ft) | Total Project Cost ($) | Cost / LF ($/LF) |
|---|---|---|---:|---:|---|---:|---:|---:|---:|
| **C01** | `CEDB` | `500` | 2 | — | 640.0 | 331.55 | 900.00 | $318,172.70 | $353.53 |
| **C02** | `CEDB` | `500` | 2 | — | 560.0 | 331.55 | 1,210.00 | $439,145.32 | $362.93 |
| **C03** | `CEDB` | `350` | 1 | — | 400.0 | 410.12 | 505.36 | $111,668.01 | $220.97 |
| **C04** | `CEDB` | `250` | 1 | — | 60.0 | 330.48 | 1,315.00 | $288,817.13 | $219.63 |
| **C05** | `RMC` | `250` | 2 | — | 200.0 | 160.00 | 390.00 | $205,461.91 | $526.83 |
| **C06** | `OH` | `250` | 1 | `1/0` | 40.0 | 196.00 | 1,958.28 | $124,272.07 | $63.46 |
| **C07** | `CEDB` | `250` | 1 | — | 120.0 | 330.48 | 680.00 | $149,124.97 | $219.30 |