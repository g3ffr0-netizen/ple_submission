The final deliverable has been updated and verified at **`/filesystem/dc_structural__foundation_strategy__blind/output/submission.zip`** (`0.0313 MB`), containing the six required files at the archive root:

1. **`foundation_geometry.json`** — Revit foundation geometry parameter file covering all six pads (`CR-P1` … `CR-P6`) and all 36 foundations (`F1` … `F5`, `W1`), conforming to `AV-CR-2604_BIM_parameter_standard.md` (Rev. 2).
2. **`ground_improvement_plan.json`** — Ground-improvement programme (`stress_concentration_ratio_ns = 10.0`, `pier_diameter_in = 30`, `rigs_mobilised = 2`, `mobilisation_cost_usd = 80000.0`, `pads_improved = ["CR-P1", "CR-P2", "CR-P4", "CR-P6"]`, `rig_assignments`, `modulus_tests`, and per-pad `stiffness_modulus_pci`, `treatment_depth_ft`, and `total_pier_lf`).
3. **`foundation_schedule.xlsx`** — Populated on `project/owner/AV-CR-2604_foundation_schedule_TEMPLATE.xlsx` with all 36 foundation rows and the exact 19 template columns (`pad`, `foundation_tag`, `foundation_type`, `B_in`, `L_in`, `thickness_in`, `bearing_depth_in`, `bearing_stratum`, `fc_psi`, `max_wcm`, `reinforcement_ratio`, integer `bar_size`, `bar_spacing_in`, `pier_count`, `pier_diameter_in`, `pier_depth_ft`, `area_replacement_ratio`, `settlement_in`, `governing_constraint`), matching `foundation_geometry.json` 1-to-1.
4. **`foundation_feasibility_matrix.md`** — Feasibility and cost optimisation matrix for all six pads and all 36 foundations, synchronised with `foundation_geometry.json` and `design_basis.md`.
5. **`constraint_register.xlsx`** — Populated on `project/owner/constraint_register_template.xlsx` with 50 rows (within the first 96 rows) registering all HARD and SOFT requirement families with exact data-room filenames and clauses/tables/equations, and marking cancelled/superseded/non-governing documents as set aside in the `resolution` column.
6. **`design_basis.md`** — Opens with the required fenced ` ```json ` summary block (`frost`, `pads`, `campus`, `rejected_criteria`), followed by the complete narrative synchronised with all cost and engineering figures.

---

### Summary of Verified Pad Decisions and Costs

| Pad ID | Building | Occupancy Class (`FS_bearing`) | Exposure Classes (`f'c`, `max w/cm`) | Selected Strategy | Bearing Depth (in) | Spread-Only Cost (USD) | Improved Cost Excl. Mob (USD) | Savings Before Mob (USD) | Governing Constraint |
|---|---|---|---|:---:|---:|---:|---:|---:|:---:|
| **`CR-P1`** | Data Hall 1 | Light Industrial, Public (`3.5`) | `F1, S1, W1, C1` (`4000 psi`, `0.50`) | `IMPROVED_SPREAD` | `42` | `$265,454.39` | `$81,812.64` | `$183,641.75` | `BEARING` |
| **`CR-P2`** | Data Hall 2 | Light Industrial, Public (`3.5`) | `F1, S2, W1, C1` (`4500 psi`, `0.45`) | `IMPROVED_SPREAD` | `42` | `$262,748.93` | `$88,571.26` | `$174,177.67` | `BEARING` |
| **`CR-P3`** | Electrical Yard Building | Light Industrial, Public (`3.5`) | `F1, S0, W1, C1` (`4000 psi`, `0.50`) | `SPREAD` | `48` | `$32,544.34` | `$55,225.26` | `-$22,680.92` | `BEARING` |
| **`CR-P4`** | Administration & Operations | Apartments, Office (`3.0`) | `F1, S1, W1, C1` (`4000 psi`, `0.50`) | `IMPROVED_SPREAD` | `42` | `$81,142.13` | `$48,736.42` | `$32,405.71` | `BEARING` |
| **`CR-P5`** | Logistics & Spares Warehouse | Warehouse (`2.5`) | `F3, S0, W1, C1` (`5000 psi`, `0.40`) | `SPREAD` | `42` | `$18,117.64` | `$36,352.80` | `-$18,235.16` | `BEARING` |
| **`CR-P6`** | Data Hall 3 | Light Industrial, Public (`3.5`) | `F1, S2, W1, C1` (`4500 psi`, `0.45`) | `IMPROVED_SPREAD` | `42` | `$225,580.81` | `$84,255.12` | `$141,325.69` | `BEARING` |

* **Mobilised Rigs**: `2` (`RIG-1`: `CR-P1`, `CR-P2`; `RIG-2`: `CR-P4`, `CR-P6`) — Mobilisation Lump Sum = **`$80,000.00`**
* **Total Phase 1 Campus Foundation Programme Cost**: **`$434,037.42`**