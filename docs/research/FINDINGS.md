# Research Campaign 3e675c0c — Air Quality Monitor + AI Respiratory-Advice Agent

**Question:** If we had an air-quality monitor, what parameters should it process, what report/data should it generate, and how could an AI agent use that data to advise people with respiratory illness?

**Max cycles:** 30 · Sources: any

---

# ★ EXECUTIVE SUMMARY & RECOMMENDATION

**Bottom line:** Build a personal air-quality monitor around the **six WHO classical pollutants**, but make its defining output **personal inhaled dose** (not ambient concentration) and its defining behavior an **anticipatory, transparent, non-diagnostic AI advisor** that escalates for respiratory patients one AQI band *earlier* than the public. The hard engineering problem is **sensor calibration**; the hard product problem is **staying in the general-wellness lane** legally.

## 1. What the monitor should measure (parameters)
- **Core (outdoor):** PM2.5, PM10, O3, NO2, SO2, CO — the six WHO classical pollutants. **PM2.5 is the most health-critical.** Include **temperature, humidity, pressure** (needed to interpret and to correct readings).
- **Indoor module (different problem):** **CO2** (~1000 ppm = ventilation flag), **radon** (EPA action 4 pCi/L ≈150 Bq/m³, no safe level), **TVOC/formaldehyde**. These are *not* in the outdoor AQI.
- **For allergic users:** a **pollen/aeroallergen feed** is required (sourced from forecast APIs, not sensors). **Ultrafine PM1/UFP** matters biologically but is unregulated + hard to sense — monitor opportunistically, don't gate advice on it.

## 2. What data/reports it should generate
A **6-layer output taxonomy**: (1) real-time per-pollutant + NowCast AQI + driving pollutant; (2) **personal exposure — time/location-resolved, activity-adjusted inhaled dose (ADD)** + cumulative AQI-hours; (3) historical trends + exceedance log vs WHO/AQI; (4) daily/weekly summaries + symptom↔exposure charts; (5) personalized alerts + next-day forecast + lag3 particulate pre-warning; (6) machine-readable export (JSON + FHIR for clinicians).
> **The differentiator vs a public AQI app is inhaled dose** — personal exposure can diverge from the nearest station by >1000×%, and dose = concentration × activity-adjusted breathing rate × time.

## 3. How the AI agent uses it to advise respiratory patients
The agent is an **anticipatory, personalized translation layer**, not a dashboard. It:
- Fuses live device readings + **NowCast** (now) + **AQI/pollen forecasts** (next-day) + the **lag structure** to **warn before symptoms** (gaseous effects hit same-day; particulate effects peak ~3 days later).
- **Attributes cause per-pollutant & weights by condition:** COPD → NO2/O3 (meta-analysis RR 1.04/1.03 per 10 µg/m³); allergic asthma/rhinitis → PM2.5 + **pollen×pollution synergy**.
- **Escalates at the respiratory "Orange" band (AQI 100+)**, not the public 150+.
- Learns **personal trigger thresholds** from the user's symptom/rescue-inhaler log (a personalized PM2.5→respiratory-rate response is already demonstrated in asthmatics).
- Converts category → **concrete action** ("run this morning, O3 is lower"; "keep your reliever handy"; "close windows, run the purifier").

## 4. Two constraints that make or break it
- **Calibration is load-bearing:** uncalibrated low-cost PM2.5 has MAE ~17 µg/m³ (bigger than the WHO 24-h limit!), reducible to ~6 with RH-aware model calibration. Correct *before* AQI/agent inference, and surface a confidence flag.
- **Guardrails are a product requirement:** frame advice as **exposure-reduction, never diagnosis/medication dosing** (this keeps it an unregulated general-wellness tool, not an FDA device); show the *basis* of every recommendation; defer to the clinician's action plan; escalate red-flag symptoms to emergency care; protect health+location data (HIPAA/GDPR).

**Confidence:** High. All 8 sub-questions + 3 emergent questions answered; core facts (pollutants, AQI, mechanisms, dose, calibration, indoor thresholds, regulatory line) are strong (multi-source, WHO/EPA/peer-reviewed). Agent-capability and per-condition action mappings are reasoned synthesis on top of that strong base (moderate). See cycle-by-cycle detail below.

---

## Derived Sub-Question Checklist (authoritative — no list in brief)
- [x] **SQ0** — Which parameters should the monitor measure? (+ health thresholds) — *cycle 0*
- [x] **SQ1** — Standards & indices: WHO AQG vs US EPA AQI vs EU AQI; how thresholds map to respiratory-risk categories & breakpoints — *cycle 1*
- [x] **SQ2** — What reports/data outputs should the system generate? (real-time, trends, personal exposure dose, alerts, exportable schema) — *cycle 4*
- [x] **SQ3** — How do specific pollutants affect respiratory illness (asthma, COPD, allergic rhinitis)? evidence for exposure→symptom links — *cycle 2*
- [x] **SQ4** — What can an AI agent do with the data to advise respiratory patients? (personalized alerts, forecasting, activity/medication guidance, symptom correlation) — *cycle 3*
- [x] **SQ5** — Data architecture/telemetry: schema, sampling, integrations (wearables, symptom logs, EHR, forecast APIs) enabling the agent — *cycle 5*
- [x] **SQ6** — Indoor parameters & low-cost sensor caveats (CO2/TVOC/radon; calibration & accuracy of consumer sensors) — *sensor accuracy cycle 5; indoor params cycle 8*
- [x] **SQ7** — Pollen/allergen & ultrafine (PM1/UFP) — worth adding for respiratory use? sensor feasibility — *cycle 8*

---

## Cycle 0 — SQ0: Parameters to measure
The monitor's scientifically-grounded core = the **six WHO "classical" pollutants**: **PM2.5, PM10, O3, NO2, SO2, CO**. Add environmental co-parameters (temperature, relative humidity, barometric pressure) needed to interpret readings and drive comfort/symptom correlation. For indoor deployments add **CO2** (ventilation proxy), **TVOC**, and optionally **radon**/**formaldehyde**.

**WHO 2021 AQG limits (respiratory-relevant thresholds):**
| Pollutant | Long-term | Short-term |
|---|---|---|
| PM2.5 | 5 µg/m³ (annual) | 15 µg/m³ (24 h) |
| PM10 | 15 µg/m³ (annual) | 45 µg/m³ (24 h) |
| O3 | 60 µg/m³ (peak season) | 100 µg/m³ (8 h) |
| NO2 | 10 µg/m³ (annual) | 25 µg/m³ (24 h) |
| SO2 | — | 40 µg/m³ (24 h) |
| CO | — | 4 mg/m³ (24 h) |

PM2.5 is the single most health-critical parameter — it penetrates the lungs and enters the bloodstream, driving COPD, asthma exacerbation, stroke and lung cancer.

Sources: [WHO — What are the AQG](https://www.who.int/news-room/feature-stories/detail/what-are-the-who-air-quality-guidelines), [WHO Q&A](https://www.who.int/news-room/questions-and-answers/item/who-global-air-quality-guidelines), [WHO 2021 AQG (IRIS)](https://iris.who.int/handle/10665/345329).

---

## Cycle 1 — SQ1: Indices & risk categories
The **US EPA AQI** is the standard layer that converts raw µg/m³ into six color-coded risk categories. Overall AQI = the **highest single-pollutant sub-index** (so the device must compute each pollutant's sub-index and surface the *driving* pollutant). AQI=100 for a pollutant equals its short-term NAAQS.

| Color | Level | AQI | Respiratory relevance |
|---|---|---|---|
| Green | Good | 0–50 | little/no risk |
| Yellow | Moderate | 51–100 | risk for unusually sensitive people |
| **Orange** | **Unhealthy for Sensitive Groups** | **101–150** | **KEY trigger — asthma/COPD may have effects; public less so** |
| Red | Unhealthy | 151–200 | public may have effects; sensitive groups more serious |
| Purple | Very Unhealthy | 201–300 | health alert, everyone |
| Maroon | Hazardous | 301+ | emergency conditions |

**Design takeaway:** a respiratory agent should escalate advice at the *Orange* band (AQI ~100+) and per-pollutant, not wait for the public "Unhealthy" (151+) threshold. Compare later against EU EAQI (1–6) and UK DAQI (1–10).

Sources: [AirNow — AQI Basics](https://www.airnow.gov/aqi/aqi-basics/), [EPA AQI Basics PDF](https://document.airnow.gov/air-quality-index-aqi-basics.pdf).

---

## Cycle 2 — SQ3: Pollutant → respiratory-illness mechanisms
A **59-study meta-analysis** (Li et al. 2016, *Int J COPD*) confirms short-term exposure to all major pollutants significantly raises COPD-exacerbation risk. Per 10 µg/m³ increase: **NO2 RR 1.04** (1.03–1.06, strongest gaseous), **O3 RR 1.03** (1.01–1.04); PM2.5/PM10 also significant; SO2/CO weaker. Shared mechanism: **oxidative stress → airway inflammation → reduced lung function**. PM2.5 is additionally linked to asthma *development* (not just exacerbation) via oxidative stress; ozone worsens asthma airway reactivity (EPA).

**Design-critical lag structure:** gaseous pollutants peak at **lag0** (same day) → alerts must be real-time; **particulates peak at lag3** (~3-day delay) → the agent can warn PM-sensitive patients *days ahead* and attribute today's symptoms to exposure ~3 days prior.

Sources: [Li et al. 2016 COPD meta-analysis](https://pmc.ncbi.nlm.nih.gov/articles/PMC5161337/), [PM2.5 & asthma / oxidative stress](https://pmc.ncbi.nlm.nih.gov/articles/PMC9001082/), [EPA — Ozone & asthma patients](https://www.epa.gov/ozone-pollution-and-your-patients-health/health-effects-ozone-patients-asthma-and-other-chronic).

---

## Cycle 3 — SQ4: What the AI agent does with the data (the payoff)
The agent is an **anticipatory, personalized translation layer**, not a dashboard. It builds on three real EPA data products: **NowCast AQI** (current-hour, adaptive averaging → real-time go/no-go), **AQI Forecast** (next-day, issued each afternoon, includes a "forecast discussion" of when pollution peaks → activity timing), and the **AirNow API** (integration feed). On top it layers: real-time alerts on *personal* thresholds; anticipatory warnings using next-day forecast + the lag3 particulate delay (warn before symptoms); per-pollutant attribution + condition weighting (COPD→NO2/O3, asthma→PM2.5/O3); activity-timing optimization (find the day's low-pollution window); symptom↔exposure correlation to learn personal triggers; plain-language action mapping; med/action reminders; and escalation logic toward "contact your clinician" on sustained high exposure + worsening symptoms. Escalation fires at the respiratory **Orange band (AQI 100+)**, not the public 150+.

Sources: [AirNow — Using the AQI (NowCast/Forecast)](https://www.airnow.gov/aqi/aqi-basics/using-air-quality-index/), [AirNow API docs](https://docs.airnowapi.org/).

---

## Cycle 4 — SQ2: Report / data outputs the system should generate
A **6-layer output taxonomy**, not a single AQI number:
1. **Real-time** — per-pollutant concentration (6 classical + CO2/TVOC indoor), NowCast AQI + driving pollutant, temp/RH/pressure.
2. **Personal exposure** — time- & location-resolved timeline, microenvironment tagging (home/transit/work), **activity-adjusted Average Daily Inhaled Dose (ADD)**, cumulative AQI-hours over threshold.
3. **Historical** — hourly/daily trends, exceedance log vs WHO AQG + AQI breakpoints, peak events.
4. **Summaries** — daily/weekly/monthly report, low-exposure windows, symptom↔exposure chart.
5. **Alerts/forecast** — personalized threshold alerts (Orange/100+), next-day forecast, lag3 particulate pre-warning.
6. **Export** — machine-readable JSON/CSV for the agent, clinician/EHR-shareable summary, API.

**Key insight:** the output that distinguishes a *personal* monitor from a public AQI app is **inhaled dose, not ambient concentration** — personal exposure can diverge from the nearest station by >1000%, and dose = concentration × PA-adjusted breathing rate × time (a jog in Moderate air can inhale more than resting in Unhealthy air). A personalized PM2.5→respiratory-rate response is already demonstrated in asthmatic adolescents, so the symptom-correlation report is evidence-backed.

Sources: [Inhaled-dose / mobility-PA exposure (Springer 2026)](https://link.springer.com/article/10.1007/s10661-026-15090-x), [Real-time personal PM2.5 system](https://www.sciopen.com/article_pdf/10.1007/s12273-024-1163-0.pdf), [PM2.5→respiratory rate in asthmatics (arXiv)](https://arxiv.org/html/2301.06300v1), [CDC AirPen wearable](https://stacks.cdc.gov/view/cdc/230307).

---

## Cycle 5 — SQ5: Data architecture & integrations (+ SQ6 sensor caveat)
A **7-layer pipeline**: (1) **sensing** (optical PM, electrochemical NO2/O3/SO2/CO, NDIR CO2, T/RH/pressure); (2) **correction/QA** — RH+T compensation, model-based calibration vs reference, drift correction, confidence flag; (3) **telemetry** (MQTT/HTTPS; record = ts, device, lat/lon, per-pollutant, T/RH, quality_flag); (4) **context fusion** (GPS microenvironment, physical-activity→breathing-rate for inhaled dose, symptom/med log); (5) **time-series storage**; (6) **reasoning** (NowCast/AQI compute, personalized threshold engine, forecast-API ingest, LLM agent); (7) **interop** (JSON schema, FHIR for EHR, REST API, alerts).

**Key insight — the correction layer is load-bearing:** uncalibrated low-cost PM2.5 has MAE ~**17.40 µg/m³** (bigger than the WHO 24h limit of 15!), dropping to ~**5.85** after model-based calibration; **humidity is the dominant confounder** (optical sensors read swelled hygroscopic particles as extra mass); PurpleAir needs the US-EPA correction (~3.4/national eq.). RH-aware calibration must sit *before* any AQI computation or agent inference, and the agent must surface a confidence flag — otherwise it over-warns in humid air and mis-times advice. This doubles as the core of SQ6's sensor-accuracy caveat.

Sources: [Multi-model LCS calibration vs BAM (MAE 17.4→5.85)](https://egusphere.copernicus.org/preprints/2026/egusphere-2025-6203/), [PurpleAir calibration under high RH](https://amt.copernicus.org/articles/17/6735/2024/), [PurpleAir CF ~3.4](https://www.mdpi.com/1424-8220/22/13/4741/xml), [Indoor LCS AutoML calibration](https://amt.copernicus.org/articles/19/603/2026/).

---

## Cycle 6 — Emergent: Safety / medical-liability guardrails
The line between an unregulated **general-wellness** product and an **FDA-regulated medical device** is drawn by **intended use and claims, not the technology** — the same code is a device or not depending on what it claims to do (21st Century Cures Act statutory exemption for software-only general-wellness). FDA's 2026 CDS guidance grants enforcement discretion only when the recommendation's **basis is transparent and independently reviewable**.

**Agent guardrails (product requirements, not a disclaimer):**
- **Intended-use framing** — exposure-reduction language ("limit outdoor exertion, keep your reliever handy"), NOT diagnosis or medication dosing. This one framing decision is what keeps the agent in the safe lane.
- **Non-diagnostic** — describe air conditions + general precautions; never declare "you're having an attack."
- **Transparency** — always show the basis (driving pollutant, threshold, confidence flag); no black-box advice.
- **Defer to clinician** — reinforce the user's written asthma/COPD action plan; never override it.
- **Emergency escalation** — red-flag symptoms (severe breathlessness, reliever failing, blue lips) → direct to emergency services immediately.
- **Uncertainty** — surface the low-cost-sensor confidence flag (SQ5); never present LCS as reference-grade.
- **Privacy** — health+location data → HIPAA/GDPR, explicit consent, on-device where possible.
- **No autonomous action** — advice only; keep an audit log of what was advised on what data.

Sources: [Morgan Lewis — FDA wellness/CDS boundaries 2026](https://www.morganlewis.com/zh-tw/blogs/asprescribed/2026/02/new-year-new-guidance-fda-revisits-wellness-and-cds-boundaries), [Mintz — FDA CDS enforcement discretion](https://www.mintz.com/insights-center/viewpoints/2791/2026-01-20-fda-flux-january-2026-newsletter), [Device vs wellness = intended use](https://rhizomeai.com/articles/software-as-a-regulated-medical-device-vs-wellness), [FDA Digital Health guidances](https://www.fda.gov/medical-devices/digital-health/guidances-digital-health-content).

---

## Cycle 7 — Emergent: Condition-specific personalization
Personalization is **two-dimensional**: (1) pollutant **weighting** and (2) **trigger set**.
- **Asthma** (reversible, often allergic; any age): drivers O3, NO2, PM2.5 **+ allergens/pollen** → pre-exertion reliever prompts, low-O3 activity timing, reinforce the written action-plan zones.
- **COPD** (irreversible, older, smoking history): NO2 & O3 strongest (RR 1.04 / 1.03 per 10 µg/m³, cycle 2) → avoid exertion during gaseous peaks, exacerbation prevention, watch dyspnea/sputum change, escalate early.
- **Allergic rhinitis** (upper-airway, allergic): dominated by the **pollen × pollution synergy** — diesel/traffic PM acts as an adjuvant and damages pollen so it releases more allergen → **requires a pollen/aeroallergen feed** (pure 6-pollutant AQI misses their main trigger); warn hardest on high-pollen + high-PM/NO2 co-days.

**Key insight:** for the allergic segment, pollen data is **not optional** — it's the primary trigger, and it interacts multiplicatively with pollution. Asthma-COPD overlap (ACO) is common, so the agent should learn a per-user trigger profile from symptom logs rather than assume a clean diagnostic box.

Sources: [Air pollution & allergic diseases](https://pmc.ncbi.nlm.nih.gov/articles/PMC3192198/), [Air pollution & pollen allergy](https://pmc.ncbi.nlm.nih.gov/articles/PMC8638356/), [Pollution & respiratory allergies](https://www.frontiersin.org/journals/allergy/articles/10.3389/falgy.2024.1521072/full), [Asthma vs COPD in primary care](https://pmc.ncbi.nlm.nih.gov/articles/PMC9683358).

---

## Cycle 8 — SQ6 indoor params + SQ7 pollen/UFP + forecasting (consolidated)
**Indoor module (distinct from outdoor AQI):** CO2 (~**1000 ppm** = inadequate-ventilation marker → headache/fatigue; a *ventilation* action signal, fix is behavioral), radon (EPA action **4 pCi/L ≈ 150 Bq/m³**, WHO ref ~100 Bq/m³, no safe level, long-term lung-cancer risk), TVOC/formaldehyde (irritant asthma triggers). None of these belong in the outdoor 6-pollutant AQI.
**Pollen & UFP (SQ7):** pollen is a **required feed** for the allergic segment (cycle-7 synergy) but is sourced from **forecast APIs**, not pollutant sensors. Ultrafine PM1/UFP penetrates deepest but is **unregulated** (WHO 2021 = good-practice statement, no numeric limit) and immature for consumer sensors → monitor opportunistically, don't gate advice on it.
**Forecasting (q428d8806):** the agent **consumes** external forecasts (AQI: AirNow/OpenAQ/IQAir; pollen: Google Pollen API) and **fuses** them with the personal reading, inhaled dose, and symptom log — it does not build an AQ model from scratch; ML symptom-risk is a personalization layer on top.

**Key insight:** indoor CO2 is a *ventilation* signal (open a window) categorically different from a pollution-*avoidance* signal — the agent must not conflate them. And the agent's forecasting value-add is *fusion*, not prediction.

Sources: [EPA radon action level](https://www.epa.gov/radon/what-epas-action-level-radon-and-what-does-it-mean), [Health Canada CO2 guideline](https://www.canada.ca/en/health-canada/services/publications/healthy-living/residential-indoor-air-quality-guidelines-carbon-dioxide.html), [CO2 IAQ guideline review (Nature)](https://www.nature.com/articles/s41370-024-00694-7), [Illinois DPH IAQ](https://dph.illinois.gov/topics-services/environmental-health-protection/toxicology/indoor-air-quality-healthy-homes/idph-guidelines-indoor-air-quality.html).

---

## Research State
**ALL sub-questions answered:** SQ0, SQ1, SQ2, SQ3, SQ5, SQ6, SQ7 (strong); SQ4 (moderate); emergent guardrails (strong), condition-personalization (moderate), forecasting (moderate). No open leads of material value remain.
**Next (cycle 9 = final):** write the **Executive Summary + Recommendation at the TOP of FINDINGS.md**, then write `worker_done.json` and call `autonudge_stop`. Deliberately finishing at 9 findings of 30 max cycles — the question is comprehensively answered with strong evidence; further cycles would add marginal depth only.
**Dead-ends:** none.
**Weak spots (minor, non-blocking):** UFP/pollen-API specifics are moderate; per-condition action mappings are clinical-standard synthesis; WHO AQG numeric table from cycle 0 uses published 2021 figures (not IRIS-PDF-verified). None change the conclusions.
