# Подгонка модели по данным опытов
> есть таблица прогонов «условия → результат», нужен оптимум, значимые факторы или прогноз

## Порядок

1. Прочитать таблицу через `read_data_file`, понять колонки и диапазоны.
2. Отделить факторы (что варьировали) от отклика (что измеряли).
3. Оценить, хватает ли данных: < 8 точек — модель не строить, сказать об этом
   прямо и предложить план эксперимента (методика `plan_eksperimenta`).
4. Подобрать модель и ОБЯЗАТЕЛЬНО проверить её на отложенных данных.
5. Назвать значимые и незначимые факторы с числами.
6. Дать оптимум и прогноз с интервалом.
7. Указать границы применимости — за пределами обученной области не предсказывать.

## Код

```python
import numpy as np, pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import cross_val_score, KFold
from sklearn.metrics import r2_score, mean_absolute_error

df = pd.read_excel("данные.xlsx")        # или pd.read_csv
X_cols = ["T_C", "time_min", "conc"]     # факторы
y_col  = "yield_pct"                     # отклик
X, y = df[X_cols].values, df[y_col].values

# мало точек — линейная модель с взаимодействиями; много — бустинг
model = LinearRegression() if len(y) < 25 else GradientBoostingRegressor(random_state=0)
cv = cross_val_score(model, X, y, cv=KFold(min(5, len(y)), shuffle=True, random_state=0),
                     scoring="r2")
model.fit(X, y)
pred = model.predict(X)

# значимость факторов
if hasattr(model, "feature_importances_"):
    weights = dict(zip(X_cols, model.feature_importances_.round(3)))
else:
    weights = dict(zip(X_cols, (model.coef_ * X.std(axis=0) / y.std()).round(3)))

print(f"точек: {len(y)}, R2 на перекрёстной проверке: {cv.mean():.2f} ± {cv.std():.2f}")
print(f"MAE: {mean_absolute_error(y, pred):.2f}")
print("вклад факторов:", weights)
print("область применимости:", {c: (float(df[c].min()), float(df[c].max())) for c in X_cols})

result = {"точек": int(len(y)), "R2_cv": round(float(cv.mean()), 3),
          "MAE": round(float(mean_absolute_error(y, pred)), 3),
          "вклад_факторов": weights,
          "область": {c: [float(df[c].min()), float(df[c].max())] for c in X_cols}}
```

## Типовые ошибки

- **R² на обучающих данных вместо перекрёстной проверки.** На 20 точках бустинг
  даст R² = 0,99 и никакой предсказательной силы. Всегда `cross_val_score`.
- **Экстраполяция.** Если запрошенные условия вне `область` — сказать, что
  прогноз недостоверен, а не выдавать число молча.
- **Молчание о незначимых факторах.** Если фактор не влияет — это ценный
  результат: его можно не варьировать в следующих опытах.

## Что сказать человеку сверх чисел

Самое полезное — **какой фактор он не варьировал вообще**. Сверьтесь с
литературой (`literature_search`): если в мировых работах выигрыш даёт фактор,
которого нет в его таблице, это и есть главный вывод.
