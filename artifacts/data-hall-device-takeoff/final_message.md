### Verified Summary of Deliverables — Device Takeoff and Priced Bill of Materials (`ec_0205_d100`)

#### 1. Deliverable Paths and Archive Verification
- **Target Submission Archive:** `/filesystem/final_batch4__electrical_task17_ec_0205_d100__blind/output/submission.zip`
- **Root Archive Members (also written to `/filesystem/final_batch4__electrical_task17_ec_0205_d100__blind/output/`):**
  1. `takeoff.csv` (`type_mark,description,quantity`) — 17 scheduled device type marks (`R1`–`R8`, `J1`, `P1`, `F1`–`F6`, `D1`)
  2. `locations.csv` (`sheet,type_mark,grid_bay,condition,count`) — aggregated location matrix across `E-201`–`E-204`, `E-211`, `E-231`–`E-233`
  3. `bom.csv` (`line_item,description,for_type_mark,condition,quantity,unit,unit_price,extended_price`) — 34 attribution rows + 17 `(PURCHASE)` / `(STOCK)` rows
  4. `register.csv` (`family,document,clause,value,how_satisfied`) — 32 rows covering all criteria clauses `E-01`–`E-22` (including `E-04b`) and conventions `C1`–`C9`
  5. `basis.md` — complete basis of estimate

---

#### 2. Scope Exclusions Verified
- Unresolved symbols appearing on any plan that are not defined in the symbol legend on E-001 are not scheduled devices: carry no quantity for them in takeoff.csv, locations.csv, or bom.csv, and list them in basis.md with sheet and grid bay.
- While-in-use extra-duty hood required for DAMP receptacles has no item in the device catalogue: state this in basis.md and carry no line item for the hood in bom.csv.
- Duct smoke detectors (D1) are furnished under Division 23 (HVAC) and installed/wired under Division 26/28: carry only the installation item (INS-DUCT-DET) and do not carry the detector itself as a material item.
- Devices shown in shaded match-line reference bands beyond the match line on quadrant plans E-201 through E-204 are shown for reference only and are counted on the adjacent sheet.
- Areas cross-referenced to enlarged plans E-231, E-232, and E-233 are drawn empty of devices on parent plans E-201 through E-204; devices in those areas are counted once from the enlarged plans.
- Duplex receptacles (R1) inside the Revision 2 cloud in grid bay A1 on E-201 are deleted per Addendum No. 02 (2026-08-07) Item 02-1 and replaced at the first and second positions from the south with one quadraplex receptacle (R2) each.

---

#### 3. Verified Device Takeoff (`takeoff.csv` & `locations.csv`)

| Type Mark | Description | E-201 | E-202 | E-203 | E-204 | E-211 | E-231 | E-232 | E-233 | Total Quantity |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `R1` | `RECEPTACLE, DUPLEX, 20 A, 125 V, WALL-MOUNTED` | 7 | 9 | 2 | 2 | 0 | 9 | 7 | 6 | **42** |
| `R2` | `RECEPTACLE, QUADRAPLEX, 20 A, 125 V, WALL-MOUNTED` | 2 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | **8** |
| `R3` | `RECEPTACLE, DUPLEX, GROUND-FAULT CIRCUIT INTERRUPTER, 20 A` | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 2 | **8** |
| `R4` | `RECEPTACLE, DUPLEX, ISOLATED GROUND, 20 A, 125 V` | 0 | 3 | 0 | 0 | 0 | 0 | 0 | 2 | **5** |
| `R5` | `RECEPTACLE, DUPLEX, WEATHER-RESISTANT, WITH EXTRA-DUTY WHILE-IN-USE COVER` | 13 | 9 | 5 | 5 | 0 | 0 | 0 | 0 | **32** |
| `R6` | `RECEPTACLE, DUPLEX, EMERGENCY BRANCH, 20 A, 125 V` | 1 | 0 | 1 | 0 | 0 | 1 | 0 | 1 | **4** |
| `R7` | `BOX, OUTLET, FLOOR, TWO-GANG, IN ACCESS FLOOR` | 0 | 3 | 0 | 3 | 0 | 0 | 0 | 0 | **6** |
| `R8` | `RECEPTACLE, SINGLE, 50 A, 250 V, WALL-MOUNTED` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `J1` | `BOX, JUNCTION, 100 MM SQUARE, CONCEALED` | 11 | 3 | 4 | 3 | 0 | 4 | 1 | 0 | **26** |
| `P1` | `BOX, PULL, 300 X 300 X 150 MM, SURFACE` | 1 | 1 | 1 | 0 | 0 | 2 | 1 | 1 | **7** |
| `F1` | `APPLIANCE, NOTIFICATION, VISIBLE, WALL-MOUNTED` | 0 | 0 | 0 | 0 | 12 | 0 | 0 | 0 | **12** |
| `F2` | `APPLIANCE, NOTIFICATION, AUDIBLE-VISIBLE, WALL-MOUNTED` | 0 | 0 | 0 | 0 | 1 | 2 | 0 | 1 | **4** |
| `F3` | `STATION, MANUAL, FIRE ALARM, WALL-MOUNTED` | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 1 | **3** |
| `F4` | `DETECTOR, SMOKE, PHOTOELECTRIC, CEILING-MOUNTED` | 0 | 0 | 0 | 0 | 6 | 0 | 0 | 0 | **6** |
| `F5` | `DETECTOR, SMOKE, PHOTOELECTRIC, IN ACCESS FLOOR` | 0 | 0 | 0 | 0 | 25 | 0 | 2 | 3 | **30** |
| `F6` | `DETECTOR, SMOKE, PROJECTED BEAM, WALL-MOUNTED` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `D1` | `DETECTOR, SMOKE, DUCT, FURNISHED UNDER DIVISION 23` | 0 | 0 | 0 | 0 | 25 | 1 | 1 | 0 | **27** |
| **Total** | | **35** | **28** | **13** | **13** | **71** | **22** | **18** | **20** | **220** |

---

#### 4. Verified Priced Bill of Materials (`bom.csv`, `product_data/price_list_2026Q2.md`)

| Line Item | Description | Required ($Q_{\text{req}}$) | Band | Unit Price ($) | Carton | Min w/ 5% Spare | Purchased ($Q_{\text{buy}}$) | Attribution Extended ($) | Purchase Extended ($) |
|---|---|---:|---|---:|---:|---:|---:|---:|---:|
| `ACC-FIRESTOP-PAD` | `PAD, FIRESTOP, INTUMESCENT, OUTLET BOX` | 25 | `24-199` | $94.06 | 10 | 27 | 30 | $2,351.50 | $2,821.80 |
| `ACC-IG-CONDUCTOR` | `CONDUCTOR, INSULATED EQUIPMENT GROUNDING, PER DEVICE` | 5 | `3-23` | $3.36 | 10 | 6 | 10 | $16.80 | $33.60 |
| `APP-HS-24` | `APPLIANCE, NOTIFICATION, HORN-STROBE, 24 V AC` | 4 | `3+` | $74.35 | 1 | 5 | 5 | $297.40 | $371.75 |
| `APP-PULL` | `STATION, MANUAL, FIRE ALARM, DOUBLE ACTION` | 3 | `2-11` | $81.06 | 1 | 4 | 4 | $243.18 | $324.24 |
| `APP-STROBE-120` | `APPLIANCE, NOTIFICATION, STROBE, RED, 120 V AC` | 12 | `4-35` | $299.44 | 1 | 13 | 13 | $3,593.28 | $3,892.72 |
| `BOX-4SQ-15` | `BOX, OUTLET, 4 IN SQUARE, 1-1/2 IN DEEP, 1/2 IN KO` | 144 | `75-749` | $1.99 | 25 | 152 | 175 | $286.56 | $348.25 |
| `BOX-FLOOR-2G` | `BOX, FLOOR, RECESSED, STEEL, TWO-GANG` | 6 | `3-23` | $104.36 | 1 | 7 | 7 | $626.16 | $730.52 |
| `BOX-MAS-4KO` | `BOX, MASONRY, 3-1/2 IN DEEP, 4 KO` | 36 | `30-249` | $22.02 | 25 | 38 | 50 | $792.72 | $1,101.00 |
| `BOX-PULL-12` | `BOX, PULL, 12 X 12 X 6 IN, SCREW COVER` | 7 | `4-29` | $34.75 | 1 | 8 | 8 | $243.25 | $278.00 |
| `CVR-4SQ-BLANK` | `COVER, 4 IN SQUARE, FLAT BLANK` | 26 | `20-199` | $0.81 | 10 | 28 | 30 | $21.06 | $24.30 |
| `DET-PHOTO` | `DETECTOR, SMOKE, PHOTOELECTRIC, CEILING, WITH BASE` | 6 | `3-23` | $116.39 | 1 | 7 | 7 | $698.34 | $814.73 |
| `DET-PHOTO-UF` | `DETECTOR, SMOKE, PHOTOELECTRIC, ACCESS FLOOR, WITH BASE` | 30 | `15-119` | $133.02 | 1 | 32 | 32 | $3,990.60 | $4,256.64 |
| `DEV-DUP-20` | `RECEPTACLE, DUPLEX, 20 A 125 V, 5-20R, SPEC GRADE` | 62 | `40-399` | $5.18 | 10 | 66 | 70 | $321.16 | $362.60 |
| `DEV-GFCI-20` | `RECEPTACLE, DUPLEX, GFCI, 20 A 125 V, WEATHER-RESISTANT` | 40 | `20-199` | $20.03 | 10 | 42 | 50 | $801.20 | $1,001.50 |
| `DEV-IG-20` | `RECEPTACLE, DUPLEX, ISOLATED GROUND, 20 A 125 V, 5-20R` | 5 | `3-23` | $24.25 | 10 | 6 | 10 | $121.25 | $242.50 |
| `INS-DUCT-DET` | `INSTALLATION OF DUCT SMOKE DETECTOR FURNISHED UNDER DIVISION 23` | 27 | `15-119` | $98.15 | 1 | 29 | 29 | $2,650.05 | $2,846.35 |
| `RING-1G-58` | `RING, PLASTER, 4 IN SQUARE, 5/8 IN RAISED, ONE DEVICE` | 93 | `40-299` | $1.09 | 25 | 98 | 100 | $101.37 | $109.00 |
| **Totals** | | | | | | | | **$17,155.88** | **$19,559.50** |

- **Verified Basket Total (Sum of Attribution Rows Extended Price):** **`$17,155.88`**
- **Verified Purchase Expenditure (Sum of `(PURCHASE)` / `(STOCK)` Rows Extended Price):** **`$19,559.50`**