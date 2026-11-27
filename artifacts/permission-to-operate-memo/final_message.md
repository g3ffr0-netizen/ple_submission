I have updated and verified `/filesystem/Corwyn_PTO_Memo.docx` and OpenProject Work Package `#47` in the **DER Program Backlog Review** project so that all four required scope exclusions, register IDs, dates, entities, and pre-PTO settlement requirements are explicitly included and verified.

---

### 1. Deliverables and Locations

1. **Engineering Review Memorandum (Word Document):**
   - **Path:** `/filesystem/Corwyn_PTO_Memo.docx`
   - **Addressed To:** Lena Kovalchuk, SOC Shift Supervisor, System Operations Center
   - **Prepared For Signature By:** Reviewing Engineer, P.E., DER Program Office (`Tomás Ibarra, P.E.`, Senior Interconnection Engineer, DER Program Office, Alleniom Utilities)
   - **Record Anchor Date:** `07/14/2025` (`2025-07-14`)
   - **Requested PTO Date Evaluated:** `08/04/2025` (`2025-08-04`)

2. **OpenProject Work Package (`DER Program Backlog Review`, Project ID `3`):**
   - **Work Package ID:** `47`
   - **Subject:** `Corwyn Office Complex PV (PXA-2023-0629) — PTO Determination and Pre-PTO Settlement Items`
   - **Status Line:** `PXA-2023-0629 (Corwyn Office Complex PV): HOLD_PENDING_PRE_PTO_REMEDIATION (CAN_ISSUE_ON_2025-08-04_UPON_CURE)`
   - **Assignee / Priority / Dates:** `S. Rennick` (Commissioning Coordinator, DER Program Office) · Priority: `High` · Start Date: `2025-07-14` · Due Date: `2025-08-04`

---

### 2. Verified Scope Exclusions (Four Matters Under Separate Review)

Both `/filesystem/Corwyn_PTO_Memo.docx` (Section 2) and OpenProject Work Package `#47` explicitly record the four excluded matters under separate review and confirm that **nothing else is under separate review**:
1. `Installation, configuration, and commissioning tests of the Company's RTU and POI recloser at the site (under separate review by SCADA/EMS Engineering and Distribution Protection Engineering)`
2. `POI recloser settings and the protection coordination study behind them (under separate review by Distribution Protection Engineering, which issued them and keeps them outside these records)`
3. `The site's queue position (under separate review by Meera Vellanki under the weekly queue reports)`
4. `The system impact study and its conditions (under separate review by Ivo Grantham's DER Planning Group, which keeps them outside these records)`

---

### 3. Summary of Determination and Items Standing in the Way on the `07/14/2025` Record

- **Posture on the `07/14/2025` Record As-Is (Unconditional Release List as of `07/14/2025`: Empty / Zero — HOLD):**
  Working strictly from the project record as it stands on `07/14/2025`, Permission to Operate (PTO) **cannot issue unconditionally on the unamended `07/14/2025` record**. If Items 1 through 5 below are not resolved before `08/04/2025`, Lena Kovalchuk must **HOLD** PTO on `08/04/2025`.
- **Feasibility for the Requested `08/04/2025` PTO Date (CAN ISSUE on `08/04/2025` Subject to Pre-PTO Cure):**
  Every impediment standing in the way on the `07/14/2025` record (**Items 1 through 5** below) **can be cured and verified before `08/04/2025`** (within the 21 calendar days / 15 business days available between `07/14/2025` and `08/04/2025`, requiring ~3–5 business days of field, configuration, and documentation work). All long-lead prerequisites (`ISA-Corwyn-Office-Complex` execution on `09/12/2024`, utility RTU and POI recloser installation/acceptance with no open equipment hold under `OG-17`, no screening or standards-conflict holds under `SRP-3` or `ERP-9`, revenue meter `RM-0629` verification on `07/08/2025`, Vexlar `VX-140C` listing on `CEL-2025-Q2` row 18, Certificate of Conformance `VX-CC-25-0522`, Witness Test `WT-0629-01` under `ENG-PRC-014 Rev 3`, and City of Wrenford Final Approval under Permit `WEP-25-0317` on `07/02/2025`) are already satisfied on the record as of `07/14/2025`. Therefore, **Permission to Operate CAN ISSUE on `08/04/2025` once Corwyn Realty Partners (with Brantley Electric Company) and Alleniom Utilities settle Items 1 through 5 prior to `08/04/2025`:**

1. **Item 1 — Utility RTU Station AC Service (`PP-1` Circuit 1 / `TX-1`) and Inverter Control Power Connected Behind the Open POI Recloser (`E-101 Rev C`; `ISA-Corwyn-Office-Complex §5.2, §6.1–§6.3, §7.2, §8.3–§8.5`; `TCS-02 §3.1, §3.4, §5(c)`; `DPS-07 §4.2(4), §5.1(b), §6.3`; `WT-0629-01`):**
   - *Action Required:* **Corwyn Realty Partners / Brantley Electric Company** must re-feed the dedicated `120 V, 20 A` RTU station service circuit (and auxiliary supply) from permanent building panel `LP-2` in the parking structure electrical room (independent of the POI recloser and `SWBD-PV`), issue revised `E-101 Rev D`, and obtain City of Wrenford AHJ (`Dolores Achebe`) sign-off under Permit `WEP-25-0317`; **SCADA/EMS Engineering (`Marcus Deele`) & SOC (`Lena Kovalchuk`)** must verify continuous station power and live valid telemetry with the POI recloser open (`3–5 business days`; feasible before `08/04/2025`).
2. **Item 2 — Per-Inverter Telemetry Refresh Rate (`5 min` vs. `≤ 4 s`) on `Monitoring and Communications Schedule E-401 Rev C` (`ISA-Corwyn-Office-Complex §4.1, §6.2, §7.2`; `TCS-02 §3.3(a), §5(b)–(c), §6.1–§6.3`; `DIS-01 Rev 4 §6`):**
   - *Action Required:* **Corwyn Realty Partners / Brantley Electric Company / Vexlar Energy** must configure `INV-C1..C5` RS-485 port 2 (`COM-RTU`) for `≤ 4 s` analog updates (`≤ 60 s` integrity poll) and issue revised `E-401 Rev D` (restricting `5 min` to cumulative `kWh` and `DAS-1`); **SCADA/EMS (`Marcus Deele`) & SOC (`Lena Kovalchuk`)** must verify `≤ 4 s` refresh cadence at SCADA (`1–2 business days`; feasible before `08/04/2025`).
3. **Item 3 — Utility RTU Enclosure `1.5 kVA` Online UPS (`Corvane OL-1500R`) `4-Hour` Ride-Through Under `20%` Cold-Ambient (`< 10 °C`) / End-of-Life Derating (`TCS-02 §3.4`; `ISA §4.1, §5.2, §7.2`; `E-101 Rev C`; `OL1500R-DS Rev 4`):**
   - *Action Required:* **Corwyn Realty Partners / Brantley Electric Company & SCADA/EMS (`Marcus Deele`)** must verify actual continuous watt load on the POI pole RTU UPS output (confirming no heater on UPS output); if `> 31.0 W` (e.g., `38 W` RTU + `12 W` modem = `50 W` $\rightarrow$ `3.88 h` new / `3.10 h` derated), install an external battery pack or higher-capacity UPS achieving `≥ 4.0 h` derated runtime and update `E-101 Rev D` (`2–4 business days`; feasible before `08/04/2025`).
4. **Item 4 — Canopy DC Column Riser (`2 in. RSC`) Fill & Bundling Derating, and NEC Article 690 Warning Labels (`E-301 Rev C` & `E-101 Rev C`; `NFPA 70 / NEC 2023 §§ 690.8(B)(1), 690.9(B), 690.13(B), 690.31(B)(1), 690.56(C), 310.15(C)(1), Ch. 9 Tables 1, 4, 5`; `WEP-25-0317`):**
   - *Action Required:* **Corwyn Realty Partners / Brantley Electric Company** must confirm/install two separate `2 in. RSC` risers per inverter column (1 per combiner circuit so each retains `236.6 A ≥ 200 A` ampacity and `1.18 in² ≤ 1.363 in²` `40%` fill instead of derating to `189.3 A < 196.9 A` and exceeding `40%` fill at `1.62–2.36 in²`), affix `NEC 690.13(B)` line-and-load warning labels, `NEC 690.31(B)(1)` `WARNING: PHOTOVOLTAIC POWER SOURCE` labels at `≤ 3 m (10 ft)` on DC trays/risers, and the `NEC 690.56(C)` rapid shutdown sign at `SWBD-PV`, issue revised `E-101 Rev D` & `E-301 Rev D`, and obtain City of Wrenford AHJ confirmation if conduit is added; **DER Program Office (`S. Rennick` / `Tomás Ibarra, P.E.`)** must verify revised schedules and field completion (`1–3 business days`; feasible before `08/04/2025`).
5. **Item 5 — Day-of-PTO Live Telemetry Confirmation (`TCS-02 §5(c)`) and SOC-Supervised First Parallel Operation (`ISA-Corwyn-Office-Complex §6.1–§6.3, §7.2`; `DPS-07 Rev 2 §6.3`; `CX-CW-01 Rev 2`):**
   - *Action Required:* **SOC Shift Supervisor (`Lena Kovalchuk`) & Corwyn Realty Partners (`Nadia Ferreira-Holt`)** must confirm valid live telemetry on the SCADA operator display at the moment of PTO issuance on `08/04/2025` (with the POI recloser open), issue written PTO, and supervise POI recloser closure and first parallel operation under SCADA supervision (executed on `08/04/2025` once Items 1–4 are cured).