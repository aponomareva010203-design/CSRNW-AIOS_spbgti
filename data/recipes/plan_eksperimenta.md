# План эксперимента
> нужно спланировать опыты: какие точки ставить, сколько и в каком порядке

## Порядок

1. Перечислить факторы и их реальные границы (спросить у человека, не выдумывать).
2. Выбрать план по числу факторов и допустимому числу опытов.
3. Выдать таблицу опытов с номерами и порядком постановки (рандомизировать!).
4. Сказать, что покажет этот план, а что не покажет.

## Выбор плана

| Факторов | Опытов возможно | План |
|---|---|---|
| 2–3 | 8–15 | полный факторный 2^k + центральные точки |
| 3–5 | 15–30 | дробный факторный 2^(k-1) — экономит вдвое, но смешивает эффекты |
| 2–4, нужна кривизна | 13–30 | Бокс-Бенкен или центральный композиционный |
| уже есть данные, нужна следующая точка | 1–5 | максимум неопределённости модели (активное обучение) |

## Код

```python
import numpy as np, itertools, pandas as pd

factors = {"T_C": (160, 210), "time_min": (30, 120), "conc": (0.02, 0.08)}
center_points = 3

levels = list(itertools.product(*[(lo, hi) for lo, hi in factors.values()]))
plan = pd.DataFrame(levels, columns=list(factors))
centers = pd.DataFrame([[(lo + hi) / 2 for lo, hi in factors.values()]] * center_points,
                       columns=list(factors))
plan = pd.concat([plan, centers], ignore_index=True)

rng = np.random.RandomState(0)
plan = plan.iloc[rng.permutation(len(plan))].reset_index(drop=True)
plan.insert(0, "опыт", range(1, len(plan) + 1))
print(plan.to_string(index=False))
result = {"опытов": len(plan), "план": plan.to_dict("records")}
```

## Следующая точка по уже имеющимся данным

```python
# точка максимальной неопределённости: где модели «расходятся» сильнее всего
from sklearn.ensemble import RandomForestRegressor
rf = RandomForestRegressor(n_estimators=100, random_state=0).fit(X, y)
grid = ...  # сетка по факторам в пределах области
spread = np.std([t.predict(grid) for t in rf.estimators_], axis=0)
best = grid[spread.argmax()]
```

## Типовые ошибки

- **Опыты в порядке возрастания температуры.** Дрейф прибора и усталость
  оператора смешаются с эффектом фактора. Порядок только случайный.
- **Нет центральных точек.** Без них не видно кривизны и не оценить
  воспроизводимость.
- **Границы «на глаз».** Если за границей факторы физически невозможны
  (кипение, разложение) — спросить, а не подставить.
