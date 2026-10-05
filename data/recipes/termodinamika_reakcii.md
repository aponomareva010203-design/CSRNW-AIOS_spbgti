# Термодинамика реакции: окно осуществимости
> нужно понять, при каких температурах реакция вообще возможна, и где граница

## Порядок

1. Записать уравнение реакции и найти вещества в базе.
2. Посчитать ΔH°, ΔS°, ΔG°(T) в диапазоне температур.
3. Найти температуру, где ΔG меняет знак — нижняя граница окна.
4. Найти, что ограничивает окно сверху: разложение реагента, конкурирующая
   реакция, давление паров, предел оборудования.
5. Построить график ΔG(T) с закрашенным окном.
6. Отдельно сказать: термодинамика разрешает — не значит, что пойдёт с
   приемлемой скоростью. Кинетика — отдельный вопрос.

## Код

```python
import numpy as np
from chemicals import CAS_from_any, MW
from thermo import Chemical

T = np.linspace(300, 700, 200)                    # К
reagents = {"ethylene oxide": -1, "water": -1}    # стехиометрия, минус = расход
products = {"ethylene glycol": 1}

def gibbs(name, T_K):
    c = Chemical(name, T=float(T_K))
    return c.Hfgm - float(T_K) * c.Sfgm          # ΔG = ΔH − T·ΔS, газовая фаза

dG = np.array([sum(n*gibbs(s, t) for s, n in {**products, **reagents}.items())
               for t in T])
feasible = T[dG < 0]
print(f"окно: от {feasible.min()-273.15:.0f} до {feasible.max()-273.15:.0f} °C"
      if len(feasible) else "ΔG > 0 во всём диапазоне — реакция неосуществима")

import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.figure(figsize=(5,3)); plt.plot(T-273.15, dG/1000)
plt.axhline(0, color="k", lw=.8); plt.xlabel("T, °C"); plt.ylabel("ΔG, кДж/моль")
plt.tight_layout(); plt.savefig("dG.png", dpi=110)

result = {"T_мин_C": round(float(feasible.min()-273.15),1) if len(feasible) else None}
```

## Типовые ошибки

- Считать ΔG при 298 К и переносить вывод на 500 К: знак может смениться.
- Забыть про фазу: для жидкофазной реакции газофазные данные дают ошибку в разы.
- Выдать «реакция возможна» без верхней границы окна — там обычно и лежит
  причина, почему в промышленности используют другой маршрут.
