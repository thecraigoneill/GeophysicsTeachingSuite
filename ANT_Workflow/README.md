This is an ANT workflow (simplified) for students to process geotechnical-scale ANT data. Single node Rayleigh wave only (at this point). 

allseis_download.py is a download module for the allseis nodes. 

allseis_xcorr4.py runs the cross-correlation on the timeseries files from each node. They are stored in folders with the node's name (eg. A009786) and then the BIN files containing each hour's data separately (format YYMMDDHH.BIN).

