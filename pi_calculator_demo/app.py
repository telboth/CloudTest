"""Streamlit-demo: beregn pi fra en geometrisk rekke."""

import numpy as np
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Pi fra en geometrisk rekke", page_icon="📐", layout="wide")

st.title("Beregn π med en geometrisk rekke")

st.markdown(
    r"""
For $|x| < 1$ er

$$
\frac{1}{1+x^2} = 1 - x^2 + x^4 - x^6 + \dots = \sum_{n=0}^{\infty} (-1)^n x^{2n}
$$

en **geometrisk rekke** med kvotient $-x^2$. Integrerer vi begge sider fra $0$ til $1$, får vi

$$
\arctan(1) = \frac{\pi}{4} = \sum_{n=0}^{\infty} \frac{(-1)^n}{2n+1} = 1 - \frac13 + \frac15 - \frac17 + \dots
$$

Summerer vi de $N$ første leddene i den geometriske rekken (og integrerer delsummen), får vi et
estimat for $\pi$ som blir bedre jo flere ledd $N$ vi tar med.
"""
)

n_terms = st.sidebar.slider(
    "Antall ledd (N) i den geometriske rekken",
    min_value=1,
    max_value=2000,
    value=20,
    step=1,
)
st.sidebar.caption(
    "Flere ledd gir et mer nøyaktig estimat, men konvergensen er treg "
    "(feilen avtar omtrent som 1/N)."
)

n = np.arange(n_terms)
terms = (-1.0) ** n / (2 * n + 1)
partial_sums = 4 * np.cumsum(terms)
pi_estimate = partial_sums[-1]
error = abs(pi_estimate - np.pi)

col1, col2, col3 = st.columns(3)
col1.metric("Estimat for π", f"{pi_estimate:.6f}")
col2.metric("Fasit (π)", f"{np.pi:.6f}")
col3.metric("Avvik", f"{error:.2e}")

fig_convergence = go.Figure()
fig_convergence.add_trace(
    go.Scatter(
        x=np.arange(1, n_terms + 1),
        y=partial_sums,
        mode="lines",
        name="Delsum (estimat for π)",
        line=dict(color="#1f77b4"),
    )
)
fig_convergence.add_hline(
    y=np.pi,
    line_dash="dash",
    line_color="firebrick",
    annotation_text="π (fasit)",
    annotation_position="bottom right",
)
fig_convergence.update_layout(
    title="Konvergens mot π etter hvert som antall ledd øker",
    xaxis_title="Antall ledd N",
    yaxis_title="Estimat for π",
    height=420,
)
st.plotly_chart(fig_convergence, use_container_width=True)

x = np.linspace(0, 1, 400)
true_curve = 1 / (1 + x**2)
k = np.arange(n_terms)
approx_curve = np.sum((-(x[:, None] ** 2)) ** k[None, :], axis=1)

fig_series = go.Figure()
fig_series.add_trace(
    go.Scatter(
        x=x,
        y=true_curve,
        mode="lines",
        name="1 / (1 + x²) (fasit)",
        line=dict(color="firebrick", dash="dash"),
    )
)
fig_series.add_trace(
    go.Scatter(
        x=x,
        y=approx_curve,
        mode="lines",
        name=f"Delsum med N = {n_terms} ledd",
        fill="tozeroy",
        line=dict(color="#1f77b4"),
    )
)
fig_series.update_layout(
    title="Den geometriske rekken: arealet under kurven fra 0 til 1 gir π/4",
    xaxis_title="x",
    yaxis_title="f(x)",
    yaxis_range=[0, 2],
    height=420,
)
st.plotly_chart(fig_series, use_container_width=True)

st.caption(
    "Det blå arealet under delsum-kurven tilsvarer estimatet for π/4 "
    f"(≈ {pi_estimate / 4:.6f}). Den stiplede kurven er den eksakte funksjonen "
    "1/(1+x²), som har areal π/4 under seg."
)
