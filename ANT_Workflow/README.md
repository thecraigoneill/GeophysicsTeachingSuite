This is an ANT workflow (simplified) for students to process geotechnical-scale ANT data. Single node Rayleigh wave only (at this point). 

allseis_download.py is a download module for the allseis nodes. 

# Cross correlation

allseis_xcorr4.py runs the cross-correlation on the timeseries files from each node. They are stored in folders with the node's name (eg. A009786) and then the BIN files containing each hour's data separately (format YYMMDDHH.BIN).

For each run  you would type in the terminal:

```bash

python allseis_xcorr4.py A0097786 A0042914 --dist 5.2 --bandpass 0.1 20 --whiten --one-bit --max-lag 0.5 -j 8 --from-date 260602 --to-date 260624

```

Here our deployment went from 02/06/2026 to 24/06/2026. Choose the date range for your data. The distance is calculated from station positions using a haversine formula (see python script in miscellaneous). 
Change your node IDs (eg. A0097786) as needed.

Note the output will overwrite things - make sure you create a new directory after each run and move your cross-correlation results there, eg.

```bash

mkdir A0097786_A0042914

mv A0097786_x_A0042914.* A0097786_A0042914

```

The sac file that is made can be imported directly in FTANos_WebApp for FTAN digitisation. 

# FTAN dispersion curve picking

Download the FTANOs package.
The webpackage contains a self-contained GUI for this. It is found here:

https://github.com/thecraigoneill/FTANos/tree/main/FTANos_WebApp_V3

The GUI can be launched with:

```python

python FTAN_Viewer.py

```

The readme has step by step instructions for this here: https://github.com/thecraigoneill/FTANos/blob/main/FTANos_WebApp_V3/README.txt

## Steps:

1. ### Run:

   ```python
   python FTAN_Viewer.py
   ```

2. ### Read SAC (or ASCII / Tromino .dat).
   dt and distance are auto-filled
   from the SAC header where available (DIST); override in the sidebar
   if needed. example_trace.sac from the FTANos paper is included.
   For a Tromino record, a "Shot" dropdown appears: choose "Stack all"
   (the default — averages every 1 s post-trigger window) or step through
   individual shots. Changing the shot resets the map/picks; recompute.

3. ### Set the FTAN band (Tmin/Tmax), velocity window, filter count and alpha,
   Then Compute Map. Toggle "Wavelet (Morlet)" for a CWT map
   instead of the traditional Gaussian multiple-filter map.

4. ### Digitize: select "Disp. Curve" and click along the dispersion ridge
   ("snap to ridge" pulls each click to the local amplitude maximum in
   that period column). Upper/Lower Bound picks can also be recorded.
   No origin/axis-calibration clicks are needed (unlike the matplotlib
   version) - clicks map directly to (T, U). Save/load .disp files
   compatible with FTANos.

5. ### Edit the structural model table and press Forward Model
   To check the table model against the map (fundamental-mode Rayleigh U and C from
   disba fill the mode table).

6. ### Invert (NM):
   Staggered Nelder-Mead simplex, as in Amoeba_crawl -
   N starting models scaled between 0.1-0.8 of the table model. The
   best model's dispersion curve is overlaid on the map and a synthetic
   seismogram is reconstructed and overlaid on the observed waveform.
   By default the editable table is NOT overwritten (your initial model
   is preserved); tick "write best solution to model table" to have the
   best model written back. Use the Initial / Best-fit toggle above the
   model table to inspect either model; the best-fit view is read-only.

7. ### MCMC:
   Starts from the NM result if one exists. Acceptance is a proper
   Metropolis-Hastings test (accept downhill; accept uphill with
   probability exp(-dL1/T)). "Adaptive" auto-tunes the temperature T to a
   20-40% acceptance rate (recommended). Defaults that work well:
   step size 0.01, step floor 0.08, alpha 0.51, beta 1.01. The profile
   panel then shows the posterior median and 68/95% percentile fans.

   Velocity bounds (min/max Vs) are enforced throughout the inversion:
   Vs is hard-clipped to the range and models pushed against a bound are
   penalised, so the geologic limits are respected.

8. ### Export:
    .disp curve, model CSV (disba format + NM std), full MCMC
   ensemble CSV, FTAN map SVG.





