The phased data-center campus 3D BIM model has been generated and verified at **`submission/model.ifc`**.

### Summary of Deliverable (`submission/model.ifc`)

1. **IFC4 Schema & Model Structure (`docs/ifc_contract.md`)**:
   - **Format**: Valid ISO-10303-21 STEP Physical File (`FILE_SCHEMA(('IFC4'))`) with SI `METRE`, `SQUARE_METRE`, and `CUBIC_METRE` units, a single `IfcProject` (`#1`), `IfcGeometricRepresentationContext` (`#10`), `IfcGeometricRepresentationSubContext` (`#11`, `'Body'`, `'Model'`), and `IfcSite` (`#12`).
   - **3D Building Solids & Storeys**: All 7 program buildings are aggregated under `IfcSite` via `IfcRelAggregates` (`#14`), modeled with `'Body'` `'SweptSolid'` `IfcExtrudedAreaSolid` representations over simple counter-clockwise closed profiles (`IfcArbitraryClosedProfileDef` / `IfcIndexedPolyCurve`), zero `BoundingBox` representations, aggregated `IfcBuildingStorey` entities with `Elevation` values within each solid's $Z$-range, and attached `DCD_Layout` property sets (`LayoutId`, `LayoutType`, `Phase`, `HeightM`, `GrossFloorAreaM2`).

2. **Building Placement & Massing Summary**:

| `LayoutId` | `LayoutType` | `Phase` | Footprint Polygon Vertices `(x, y)` (m) | Footprint Area ($\text{m}^2$) | Storeys (`Elevation` m) | Extrusion $z_0$, Depth (m) | `HeightM` (m) | `GrossFloorAreaM2` ($\text{m}^2$) |
| :--- | :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: |
| **`DH_P1`** | `data_hall` | `1` | `(90, 20), (166, 20), (166, 80), (90, 80)` | `4,560.0` | 2 (`0.0`, `3.6`) | `0.0`, `7.2` | `7.2` | `9,120.0` |
| **`DH_P2`** | `data_hall` | `2` | `(90, 190), (166, 190), (166, 250), (90, 250)` | `4,560.0` | 2 (`0.0`, `3.6`) | `0.0`, `7.2` | `7.2` | `9,120.0` |
| **`SUB`** | `substation` | `1` | `(190, 172), (230, 172), (230, 198), (190, 198)` | `1,040.0` | 1 (`0.0`) | `0.0`, `10.0` | `10.0` | `1,040.0` |
| **`CTP`** | `cooling_plant` | `1` | `(214, 232), (244, 232), (244, 260), (214, 260)` | `840.0` | 1 (`0.0`) | `0.0`, `3.5` | `3.5` | `840.0` |
| **`GEN`** | `generator_yard` | `1` | `(274, 232), (294, 232), (294, 258), (274, 258)` | `520.0` | 1 (`0.0`) | `0.0`, `3.5` | `3.5` | `520.0` |
| **`BRM`** | `berm` | `1` | `(204, 224), (294, 224), (294, 230), (210, 230), (210, 260), (204, 260)` | `720.0` | 1 (`0.0`) | `0.0`, `12.0` | `12.0` | `720.0` |
| **`RET`** | `stormwater_retention` | `1` | `(190, 20), (262, 20), (262, 45), (190, 45)` | `1,800.0` | 1 (`0.0`) | `-0.3`, `0.3` | `0.0` | `1,800.0` |

3. **Networks & Connectivity Graph (`DCD_Networks` on `IfcSite`)**:
   - **`road_p1`** (Phase 1, `width_m = 6.0`): `[[178.0, 0.0], [178.0, 218.0], [274.0, 218.0]]` ($314.0\text{ m}$). Originates at the single south frontage entrance `[178.0, 0.0]`, passes through the central upland corridor clearing both `WET1` and `WET2` buffers by $\ge 15.0\text{ m}$, and brings all Phase 1 structures within $\le 14.0\text{ m}$ ($\le 18.0\text{ m}$ required).
   - **`road_p2`** (Phase 2, `width_m = 6.0`): `[[178.0, 218.0], [170.0, 218.0]]` ($8.0\text{ m}$). Joins `road_p1` at `[178.0, 218.0]` and serves `DH_P2` within $4.0\text{ m}$.
   - **`mv_p1_util`** (Phase 1): `[[320.0, 195.0], [230.0, 172.0]]` ($92.89\text{ m}$), routing from `POI = [320.0, 195.0]` to `SUB`.
   - **`mv_p1_dh1`** (Phase 1): `[[230.0, 172.0], [166.0, 80.0]]` ($112.07\text{ m}$), routing from `SUB` to `DH_P1`.
   - **`mv_p2_dh2`** (Phase 2): `[[230.0, 171.5], [185.0, 171.5], [166.0, 190.0]]` ($71.52\text{ m}$), routing from $0.5\text{ m}$ south of `SUB`'s SE corner (`distance = 0.5 m` $\le 2.0\text{ m}$ join tolerance and $\le 5.0\text{ m}$ connection tolerance, while remaining strictly outside `SUB`'s Phase 1 footprint) to `DH_P2`.

4. **Verification of All 17 Binding Requirements**:
   - **Program, GFA, Aspect Ratios & Floor-to-Floor Heights (Sec. 1, A.1, A.3, A.5)**: `DH_P1` (south) and `DH_P2` (north, $Y \ge 190\text{ m}$) each have $4,560\text{ m}^2 \ge 4,500\text{ m}^2$ footprint, aspect ratio $1.27 \le 2.5$, 2 storeys with floor-to-floor height $3.6\text{ m} \in [3.2, 5.5]\text{ m}$, and GFA $9,120\text{ m}^2 \ge 7,200\text{ m}^2$. All other buildings exceed their minimum areas.
   - **Heights, Setbacks & Lot Coverage (Sec. 2.2, 2.3, 2.5, A.4)**: Max height is $12.0\text{ m} \le 13.8\text{ m}$; all structures are set back $\ge 20.0\text{ m} \ge 18.0\text{ m}$ from all parcel boundaries; building lot coverage is $7.77\%$ in Phase 1 and $12.86\%$ at full build-out ($\le 15.1\%$).
   - **Fire Access, Single Entrance & Hazard Separation (Sec. 3.1, 3.2, 3.3, 8.4, Sched. B & C)**: Single entrance at `(178.0, 0.0)`; all structures lie within $\le 14.0\text{ m} \le 18.0\text{ m}$ of the road network; `GEN` is separated by $\ge 30.0\text{ m} \ge 28.0\text{ m}$ from all occupied structures (`CTP`, `SUB`, `DH_P1`, `DH_P2`).
   - **Wetland Exclusions & Stormwater Retention (Sec. 4.1, 4.2, 4.4)**: All structures and roads clear `WET1` and `WET2` by $> 10.0\text{ m}$ ($> 8.0\text{ m}$ buffer); `RET` provides $1,800.0\text{ m}^2 \ge 1,587.3\text{ m}^2$ ($11.8\%$ of total impervious roof + road area).
   - **Acoustic & 3D Visual Screening (Sec. 5.1, 5.2, A.2, A.6)**: The $12.0\text{ m}$ tall L-shaped Phase 1 `BRM` intercepts 100% of 3D lines of sight from `R1 = (-25, 140)` and `R2 = (160, -30)` to `GEN` and `CTP`. Cumulative noise is $36.61\text{ dB(A)}$ (`R1`) and $38.35\text{ dB(A)}$ (`R2`) in Phase 1, and $37.14\text{ dB(A)}$ (`R1`) and $38.37\text{ dB(A)}$ (`R2`) at full build-out ($\le 42.35\text{ dB(A)}$).
   - **MV Continuity, Phasing & Design-to-Cost Caps (Sec. 6.2–6.5, 7.1–7.5)**:
     - **Phase 1 Weighted Cost**: $1.0 \times 204.96 + 0.3 \times 314.00 = \mathbf{299.16 \le 314.0}$.
     - **Full Build-Out Weighted Cost**: $1.0 \times 276.48 + 0.3 \times 322.00 = \mathbf{373.08 \le 473.0}$.