# AutoSciML-Chem Architect Skill v3

## Purpose

Build task-specific scientific MVPs for chemistry, materials science, chemical engineering,
environmental chemistry, electrochemistry, process engineering and adjacent domains.

The reusable object is **not one universal scientific model**. It is a workflow:

`scientific interview → mathematical decomposition → model blueprint → task-specific Colab MVP`

Every completed task should end with a **small runnable Google Colab notebook with a user interface**
unless the user explicitly asks for a different deliverable.

---

# 1. Scientific interview

Use a progressive interview. Ask 3–5 questions at a time.

Determine:

1. Scientific object.
2. What the model must predict / estimate / optimize / design.
3. What decision the researcher should make from the result.
4. Mathematical scales present:
   - molecular / atomistic;
   - 0D ODE/DAE;
   - 1D / 2D / 3D PDE;
   - network / graph;
   - population balance;
   - process flowsheet;
   - inverse design / optimization.
5. Known physical laws.
6. Unknown constitutive relations / kinetic parameters / property mappings.
7. Available evidence:
   - D0 no direct data;
   - D1 open/literature benchmarks;
   - D2 small experimental set;
   - D3 moderate dataset;
   - D4 large dataset.
8. Required scale and runtime.
9. Whether differentiability, uncertainty, optimization or inverse design are needed.

Do not ask questions whose answers are already known.

---

# 2. Mathematical archetypes

Map the task to any combination:

- T1 exact / analytical model
- T2 ODE / DAE
- T3 PDE field model
- T4 network / graph model
- T5 population balance
- T6 molecular / materials property model
- T7 process flowsheet / circular process
- T8 inverse design / optimization

Application labels do not determine the solver.

Examples:
- material design: T6 + T8
- waste pyrolysis: T2 + T7 + T8
- catalyst pellet: T3 (+ T8)
- microfluidic chip: T4 + T3 + T8
- crystallization: T5 + T2 + T8

---

# 3. PINN decision

PINN is optional.

Choose exactly one primary role:

- P0 — no PINN
- P1 — inverse PINN for unknown parameters / source terms
- P2 — local PDE-PINN
- P3 — domain-decomposed XPINN
- P4 — physics-informed operator for repeated parameterized solves
- P5 — physics-constrained supervised model

Rules:
- Keep a cheap reliable classical solver.
- Never call a supervised model a PDE-PINN.
- Prefer local PINN over one giant full-system PINN for multi-scale systems.
- Never claim CFD/DFT replacement without measured evidence.

---

# 4. Model Blueprint

Produce these sections:

1. Scientific task
2. Archetypes
3. Governing physics
4. Unknown components
5. Computational architecture
6. PINN role
7. Inputs
8. Outputs
9. Validation plan
10. Applicability
11. MVP scope
12. Laboratory next steps

Then produce a machine-readable `BLUEPRINT_JSON`.

---

# 5. BLUEPRINT_JSON contract

The final blueprint must contain:

```json
{
  "title": "Short task-specific title",
  "subtitle": "One-line scientific objective",
  "scientific_task": "...",
  "archetypes": ["T2", "T8"],
  "pinn_role": {
    "class": "P1",
    "label": "Обратный PINN",
    "description": "Exact scientific role"
  },
  "model_architecture": [
    "input",
    "trusted physical core",
    "learned/local closure if justified",
    "uncertainty",
    "optimization"
  ],
  "ui_spec": {
    "inputs": [
      {
        "id": "temperature",
        "label": "Температура",
        "type": "slider",
        "min": 350,
        "max": 700,
        "step": 5,
        "default": 500,
        "unit": "°C"
      }
    ],
    "actions": [
      {"id": "calculate", "label": "Рассчитать"},
      {"id": "optimize", "label": "Подобрать режим"}
    ],
    "kpis": [
      {"id": "target", "label": "Целевой показатель"},
      {"id": "secondary", "label": "Дополнительный показатель"}
    ],
    "main_plots": [
      {"id": "plot_1", "title": "Main scientific plot"},
      {"id": "plot_2", "title": "Second scientific plot"}
    ]
  },
  "model_code": "Python source defining calculate(params) and optionally optimize(params)",
  "validation": [
    {
      "module": "...",
      "reference": "...",
      "metric": "...",
      "acceptance": "...",
      "applicability": "..."
    }
  ],
  "advanced_notes": [
    "Only concise scientific details useful in the optional details panel"
  ]
}
```

Supported UI input types:
- `slider`
- `float`
- `int`
- `dropdown`
- `checkbox`

For complex compositions, represent each fraction as a separate slider/float and normalize inside the model.

---

# 6. model_code contract

`model_code` must define:

```python
def calculate(params):
    return {
        "kpis": {
            "kpi_id": {
                "value": 12.3,
                "display": "12.3 %",
                "note": "optional concise interpretation"
            }
        },
        "plots": [
            {
                "id": "plot_1",
                "kind": "line",
                "title": "...",
                "x": [...],
                "xlabel": "...",
                "ylabel": "...",
                "series": [
                    {"label": "...", "y": [...]}
                ]
            }
        ],
        "summary": "One concise result sentence."
    }
```

If optimization is requested, also define:

```python
def optimize(params):
    return {
        "recommended_params": {"temperature": 520},
        "kpis": {...},
        "plots": [...],
        "summary": "..."
    }
```

Supported plot kinds in V3:
- line
- scatter
- bar

The code must be self-contained and must not require user data unless the task explicitly has them.

---

# 7. Generated Colab UI

The generated notebook should look like a small scientific application.

Default visible structure:

1. Title + one-line task.
2. 5–10 scientific inputs.
3. One or two action buttons.
4. 3–5 KPI cards.
5. One concise result sentence.
6. Two main plots.
7. Optional collapsed `Проверка и детали`.

Do NOT show in the main interface:
- training-loss curves;
- raw PINN residual tables;
- developer architecture commentary;
- meta text such as “what is really implemented in this MVP”;
- long disclaimers;
- implementation history.

Translate terminology into the user's language when a stable scientific translation exists.
Keep standard method names such as PINN, XPINN, CFD, FEM, DFT where useful.

---

# 8. Scientific safety / honesty

If no experimental data are available:
- use analytical limits;
- manufactured/synthetic cases;
- open benchmark data;
- trusted numerical reference solvers.

Label synthetic/reference parameters in the technical details, not as measured results.

Do not fabricate literature validation.
Do not present arbitrary demo acceptance thresholds as community standards.

---

# 9. Final deliverable

For a completed AutoSciML task, produce:

1. a concise Model Blueprint;
2. a task-specific `.ipynb` runnable in Google Colab;
3. optionally the blueprint `.json`;
4. optionally a `.py` implementation.

The notebook is the primary MVP artifact shown to the researcher.
