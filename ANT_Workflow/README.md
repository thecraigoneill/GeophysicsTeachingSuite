This is an ANT workflow (simplified) for students to process geotechnical-scale ANT data. Single node Rayleigh wave only (at this point). 

allseis_download.py is a download module for the allseis nodes. 

# Cross correlation

allseis_xcorr4.py runs the cross-correlation on the timeseries files from each node. They are stored in folders with the node's name (eg. A009786) and then the BIN files containing each hour's data separately (format YYMMDDHH.BIN).

For each run  you would type in the terminal:

```python allseis_xcorr4.py A0097786 A0042914 --dist 5.2 --bandpass 0.1 20 --whiten --one-bit --max-lag 0.5 -j 8 --from-date 260602 --to-date 260624```

Here our deployment went from 02/06/2026 to 24/06/2026. Choose the date range for your data. The distance is calculated from station positions using a haversine formula (see python script in miscellaneous). 
Change your node IDs (eg. A0097786) as needed.

Note the output will overwrite things - make sure you create a new directory after each run and move your cross-correlation results there, eg.

```mkdir A0097786_A0042914

mv A0097786_x_A0042914.* A0097786_A0042914 ```

The sac file that is made can be imported directly in FTANos_WebApp for FTAN digitisation. 

# FTAN dispersion curve picking

Download the FTANOs package.
The webpackage contains a self-contained GUI for this. It is found here:

https://github.com/thecraigoneill/FTANos/tree/main/FTANos_WebApp_V3

The GUI can be launched with:
python FTAN_Viewer.py

The readme has step by step instructions for this here: https://github.com/thecraigoneill/FTANos/blob/main/FTANos_WebApp_V3/README.txt




