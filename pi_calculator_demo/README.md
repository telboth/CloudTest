# Pi fra en geometrisk rekke

En liten Streamlit-demo som beregner π ved å summere ledd i en geometrisk rekke.

## Matematikk

`1/(1+x²) = 1 - x² + x⁴ - x⁶ + ...` er en geometrisk rekke med kvotient `-x²`.
Integrert fra 0 til 1 gir den `arctan(1) = π/4 = 1 - 1/3 + 1/5 - 1/7 + ...`.
Appen summerer de `N` første leddene (`N` styres med en slider) og viser:

- estimatet for π, fasiten, og avviket mellom dem
- en graf som viser hvordan estimatet konvergerer mot π når N øker
- en graf som viser selve den geometriske rekken (delsum-kurven) mot den
  eksakte funksjonen 1/(1+x²), der arealet under kurven tilsvarer π/4

## Kjøre appen

```bash
pip install -r requirements.txt
streamlit run app.py
```
