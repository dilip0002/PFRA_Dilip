import os
import io
import re
import csv
import json
import time
import urllib
import shutil
import logging
import operator
import warnings
import pathlib as pl
import papermill as pm
import scrapbook as sb
from zipfile import ZipFile
from datetime import datetime
logging.basicConfig(level=logging.ERROR)
from collections import Counter, OrderedDict
from IPython.display import display, Markdown

import numpy as np
import pandas as pd
from scipy import stats
#from nptyping import Array
from scipy.optimize import minimize
from scipy import interpolate, integrate
from matplotlib import pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.ticker import FormatStrFormatter

import fiona
import rasterio
from pyproj import Proj
import geopandas as gpd
from rasterio.mask import mask
from shapely.geometry import mapping

geoDF = 'GeoDataFrame'
plib = 'pathlib.Path'

AEC_METERS = ("+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=37.5 +lon_0=-96 "
              "+x_0=0 +y_0=0 +ellps=GRS80 +datum=NAD83 +units=m +no_defs")


#---------------------------------------------------------------------------#

'''Functions called by EventTable.ipynb. This notebook 
   calculates excess rainfall by first randomly selecting a precipitation 
   recurrance interval and corresponding precipitation amount, precipitation
   temporal distribution, and curve number for the area of interest. The 
   randomly selected precipitation data and curve number are then used by the
   curve number approach to calculate the excess rainfall amount for the 
   corresponding recurrance interval. The procedure is repeated for the 
   specified number of events/recurrance intervals. 
'''

#---------------------------------------------------------------------------#


def check_attributes(gdf: geoDF) -> None:
    '''Checks the passed geodataframe to make sure that "Volume" and "Region"
       are not attributes, else the intersect_temporal_areas function will 
       fail. 
    '''
    assert 'Volume' and 'Region' not in list(gdf.columns), ('"Volume" and '
        '"Region" cannot be columns in the vector polygon. Rename columns '
        'and reload')


def intersect_temporal_areas(geo_df: geoDF, datarepository_dir: plib, 
           Temporal_area_filename: str, alldata: bool=False, 
           projected_crs: str = AEC_METERS) -> (dict, geoDF):
    '''Intersects the area of interest with the NOAA Atlas 14 volumes and 
       regions. The volume, region, and percent area of the area of interest 
       is returned in a dictionary. If alldata is set to True, the dictionary
       returned will contain information for all volumes and regions that 
       interesect the area of interest. The keys 'Volume', 'Region', and 
       'Percent_area' will always represent the NOAA Atlas 14 volume and 
       region that has the largest intersection with the area of interest.
    '''
    vol_gdf = gpd.read_file(datarepository_dir/Temporal_area_filename)
    vol_gdf.to_crs(AEC_METERS, inplace = True)
    geo_df_projected = geo_df.to_crs(AEC_METERS)
    intersection = gpd.overlay(geo_df_projected, vol_gdf, how='intersection')
    intersection['area'] = intersection['geometry'].apply(lambda x: x.area)
    t_area = sum(intersection['area'])
    intersection['p_area'] = intersection['area'].apply(lambda x: x/t_area*100)
    intersection = intersection.sort_values('p_area', ascending=False)
    intersection = intersection.reset_index(drop=True)
    d = {}
    for i in intersection.index:
        if i == 0:
            d['Volume'] = intersection.loc[i, 'Volume']
            d['Region'] = intersection.loc[i, 'Region']
            d['Percent_area'] = intersection.loc[i, 'p_area']
        elif i>0 and alldata:
            d[f'Volume_{i}'] = intersection.loc[i, 'Volume']
            d[f'Region_{i}'] = intersection.loc[i, 'Region']
            d[f'Percent_area_{i}'] = intersection.loc[i, 'p_area']
    for k,v in d.items():
        print('{:<17s}{:>1s}'.format(str(k),str(v)))
    intersection.to_crs(geo_df.crs, inplace = True)    
    return OrderedDict(d), intersection


def get_volume_code(datarepository_dir: str, vol_code_filename: str, 
                                        vol: int, sub_vol: int = None) -> str:
    ''' Extracts the NOAA Atlas 14 volume code for the specified volume number.
    '''
    if vol==5: assert sub_vol!=None, 'For Volume 5, specify sub-volume number'
    orig_dir = os.getcwd()
    os.chdir(datarepository_dir)   
    with open(vol_code_filename) as json_file:  
        vol_code = json.load(json_file)
    code = vol_code[str(vol)]
    if vol == 5: code = code[str(sub_vol)]
    os.chdir(orig_dir)
    print('NOAA Atlas 14 Volume Code:', code)
    return code


def build_precip_table(geo_df: geoDF, all_zips_list: list, noaa_url: str, 
    vol_code: str, num_attempts: int=10, verbose: bool=True) -> pd.DataFrame:
    '''Calculates the area-averaged precipitation for each return frequency 
       and duration contained within the list of zipfiles.
    '''
    start = time.time()
    results = []
    for i, zip_name in enumerate(all_zips_list):
        remote_file = os.path.join(noaa_url, zip_name)
        get_remote_file = True
        count = 1
        while get_remote_file and count<=num_attempts:
            try:
                open_socket = urllib.request.urlopen(remote_file)
                get_remote_file = False
            except:
                if verbose: print("Unable to get data on attempt {1} for "
                                                "{0}".format(zip_name, count))
                count+=1
        memfile = io.BytesIO(open_socket.read())
        with ZipFile(memfile, 'r') as openzip:
            gridfiles = openzip.namelist()
            mes = "Expected to find 1 file, found {0}".format(len(gridfiles))
            assert len(gridfiles) == 3, mes
            local_file = gridfiles[0]
            f = openzip.open(local_file)
            content = f.read() 
            local_file_disk = os.path.join(os.getcwd(), local_file)
            with open(local_file_disk, 'wb') as asc:
                asc.write(content)
        grid_data = parse_filename(zip_name, vol_code)
        try:
            grid_data['value'] = get_masked_mean_atlas14(geo_df, local_file_disk)   
            results.append(grid_data)
            os.remove(local_file_disk)
            if verbose: 
                print(i, zip_name)
        except ValueError as e:
            if 'Input shapes do not overlap' in str(e):
                print(f'{e} - if two volumes were identified, try using the other volume')
                os.remove(local_file_disk)
                raise e
    df = pd.DataFrame.from_dict(results)
    assert df.isnull().values.any()!=True, 'NaN in results dataframe'
    if verbose: 
        print(round(time.time()-start), 'Seconds')
        print(display(df.head()))
    return df


def parse_filename(zip_name: str, reg: str) -> dict:
    '''Builds a dictionary with the region, recurrance interval, duration, 
       and statistic type using the zip_name and region.
    '''
    dic = {'a': 'Expected Value', 'al': 'Lower (90%)', 'au': 'Upper (90%)'}
    reg = zip_name[0:re.search("\d", zip_name).start()]
    TR = zip_name.split(reg)[1].split('yr')[0]
    dur = zip_name.split('yr')[1].split('a')[0]
    stat = zip_name.split(dur)[1].replace('.zip','')
    grid_data = {'region':reg, 'TR':TR, 'duration':dur, 'statistic':dic[stat]}
    return grid_data    


def get_masked_mean_atlas14(gdf: geoDF, raster: str) -> float:
    '''Masks the Atlas 14 precipitation raster by the passed polygon and then 
       calculates the average precipitation for the masked polygon.
    '''
    geoms = gdf.geometry.values
    geoms = [mapping(geoms[0])]
    with rasterio.open(raster) as src:
        out_image, out_transform = mask(src, geoms, crop=True)
        raw_data = out_image[0]
        region_mean = raw_data[raw_data != src.nodatavals ].mean()
    mean_m = region_mean*0.001    
    return mean_m


def get_input_data(precip_table_dir: str, duration: int, lower_limit: int=2,
                                 display_print: bool=True) -> pd.DataFrame:
    '''Extracts the precipitation frequency data for the specified duration
       from an Excel sheet and returns the dataframe with the data.  
    '''
    area_precip = 'AreaDepths_{}hr'.format(duration)
    df = pd.read_excel(precip_table_dir, sheet_name= area_precip, index_col=0)
    df_truncated = df[df.index >= lower_limit]
    if display_print: print(display(df_truncated.head(2)))
    return df_truncated


def get_volume_region(precip_table_dir: str, vol_col: str='Volume', 
                    reg_col: str='Region', display_print: bool=True) -> list:
    '''Extracts the NOAA Atlas 14 volume and region from the Excel file 
       created by PrecipTable.ipynb
    '''
    df = pd.read_excel(precip_table_dir, sheet_name = 'NOAA_Atlas_MetaData')
    vol =df[vol_col][0]
    reg = df[reg_col][0]
    results = [vol, reg]
    if display_print: print('NOAA Atlas 14: Volume {}, Region {}'.format(vol, reg))
    return results


def get_temporal_map(data_dir: str, filename: str, vol: int, reg: int, 
                                dur: int, display_print: bool=True) -> dict:
    '''Reads the json file containing the temporal distribution data metadata
       and returns the data map and number of rows to skip for the specified
       volume, region, and duration. 
    '''
    with open(data_dir/filename) as json_file:  
        all_map = json.load(json_file)
    sliced_map = all_map[str(vol)]['regions'][str(reg)]['durations'][str(dur)]
    if display_print: print(sliced_map)
    return sliced_map


def get_temporals(temporal_dir: str, vol: int, reg: int, dur: int, 
                    qmap: dict, display_print: bool=True) -> pd.DataFrame:
    '''Reads the csv file containing the temporal distributions for the
       specified volume, region, and duration. Rows with NaNs for an index 
       are dropped. Data was downloaded from:
       https://hdsc.nws.noaa.gov/hdsc/pfds/pfds_temporal.html
    '''
    f = 'Temporals_Volume{0}_Region{1}_Duration{2}.csv'.format(vol, reg, dur)
    path = temporal_dir/f
    s = qmap['skiprows']
    df = pd.read_csv(path, skiprows = s, index_col = 0, keep_default_na=False)
    df = df[df.index!=''].copy()
    for col in df.columns:
        if 'Unnamed' in col:
            del df[col]
    if display_print: print(display(df.head(2)))
    return df


def get_quartile_rank(data_dir: str, filename: str, vol: int, reg: int, 
                                dur: int, display_print: bool=True) -> list:
    '''Extracts the quartile ranks for the specified volume, region, and
       duration. The quartile rank corresponds to the percentage of 
       precipitation events whose temporal distributions are represented
       by those in a specific quartile.
    '''
    input_data = data_dir/filename
    sheet = 'NOAA Atlas 14 Vol {0}'.format(vol)
    df = pd.read_excel(input_data, sheet_name=sheet, index_col=0)
    rank=list(df[(df.index==dur)  & (df['Region']==reg)].values[0])[1:5]
    rank_per = []
    for i in rank: 
        rank_per.append(i/100.0)
    total = sum(rank_per)
    assert 0.99 <= total <= 1.01, 'Sum of ranks not between 99% and 101%' 
    if display_print: print(rank_per)
    return rank_per


def get_duration_weight(data_dir: str, filename: str, vol: int, reg: int, 
                                dur: int, display_print: bool=True) -> list:
    '''Extracts the duration weight for the specified volume, region, and
       duration. The duration weight corresponds to the percentage of 
       precipitation events with the specified duration.
    '''
    input_data = data_dir/filename
    sheet = 'NOAA Atlas 14 Vol {0}'.format(vol)
    df = pd.read_excel(input_data, sheet_name = sheet, index_col=0)
    w=df[(df.index==dur)  & (df['Region']==reg)]['Duration Weight'].values[0]  
    if display_print: print(w)
    return w


def get_CN(pluvial_params_dir: str, BCN: str, 
                                            display_print: bool=True) -> int:
    '''Extracts the curve number for the specified area of interest from the
       pluvial parameters table. 
    '''
    df = pd.read_excel(pluvial_params_dir, sheet_name = 'Pluvial_Domain')
    pp = df[df['Pluvial Domain']==BCN]
    if display_print: print(display(pp))
    CN = int(np.round((pp['Curve Number'].values[0])))
    return CN


def get_CN_distribution(data_dir: str, filename: str, 
                                CN: int, display_print: bool=True) -> dict:
    '''Open the json file containing the curve number values for different
       antecedent moisture conditions and return the values for the 
       specified curve number.
    '''
    with open(data_dir/filename) as json_file:  
        arc_data = json.load(json_file)
    arc_data_CN = arc_data[str(CN)]  
    if display_print: print(arc_data_CN)
    return arc_data_CN


def extrap_add_ari(df: pd.DataFrame, 
                                display_print: bool=True) -> pd.DataFrame:
    '''Calls the add_ari function to update the dataframe and 
       then calls the extrapolate_extremes function in order to extrapolate 
       the confidence limits and expected value of the precipitation amount 
       for the 2000 and 3000 year return periods.
    '''
    aep='Ann. Exc. Prob.'
    ari='ARI'
    log10_ari='Log10_ARI'
    lowlim='Lower (90%)'
    ev='Expected Value'
    uplim='Upper (90%)'
    rps = [2000, 3000]
    df.loc[rps[0]] = None
    df.loc[rps[1]] = None
    df=add_ari(df)
    ycols = [lowlim, ev, uplim]
    for rp in rps:
        for ycol in ycols:
            df.loc[rp, ycol] = extrapolate_extremes(df, rp, ycol)
    if display_print: print(display(df))        
    return df


def add_ari(df: pd.DataFrame) -> pd.DataFrame:
    '''Calculates the annual exceedance probability (AEP), 
       average recurrance interval (ARI), and log of the ARI and adds the 
       results to the original dataframe.
    '''
    aep='Ann. Exc. Prob.'
    ari='ARI'
    log10_ari='Log10_ARI'
    df[aep] = 1/df.index
    df[ari] = -1/(np.log(1-df[aep]))
    df[log10_ari] = np.log(df[ari])
    return df


def extrapolate_extremes(df: pd.DataFrame, rp: int, ycol: str) -> float:
    '''Extrapolates the ycol for the specified return period. 
    '''
    xcol='Log10_ARI'
    x =df.loc[500:1000, xcol].values
    y =df.loc[500:1000, ycol].values
    f = interpolate.interp1d(x, np.log(y), fill_value='extrapolate')
    return np.exp(f(np.log(rp)))


def generate_random_samples(samplesize: int, seed: int=None, 
                                display_print: bool=True) -> pd.DataFrame:
    '''Selects the specified number of random samples from a continuous 
       normal distribution, calculates the inverse of the sample, and saves
       the results in a dataframe with column "Tr", where "Tr" is the 
       recurrance interval.
    '''
    if not seed:
        seed = np.random.randint(low=0, high=10000)
    np.random.seed(seed)
    et = pd.DataFrame()
    et['Tr'] =  1/np.random.random(samplesize) #Return Period
    et = et.sort_values(by='Tr')
    et.set_index('Tr', inplace=True)
    if display_print: print('Seed - Recurrance Interval:', seed)
    return et


def Truncate_Random_Events(r_events: pd.DataFrame, lower_limit: int=2, 
                                    upper_limit: int=3000) -> pd.DataFrame:
    ''' Removes events with recurrance intervals less than the lower_limit
        (typically 2 years) and sets recurrance intervals greater than the 
        upper limit (typically 3000 years) eqaul to the upper limit.
    '''
    use_r_events=r_events[(r_events.index >= lower_limit)].copy()
    idx_as_list=use_r_events.index.tolist()
    for i,val in enumerate(idx_as_list):
        if val>upper_limit:
            idx_as_list[i]=upper_limit
    use_r_events.index=idx_as_list
    return use_r_events


def events_table_random(raw_precip: pd.DataFrame, events_table: 
                                                pd.DataFrame)-> pd.DataFrame:
    '''Calls the add_ari function to update the dataframe and then calls the 
       scipy_interp function in order calculate the expected value, lower 
       (90%) confidence limits, and upper (90%) confidence limits for the 
       events_table given the raw_precip dataframe.
    '''
    events_table = events_table.copy()
    events_table = add_ari(events_table)
    events_table = scipy_interp(raw_precip, events_table)
    events_table = scipy_interp(raw_precip, events_table, ynew='Lower (90%)')
    events_table = scipy_interp(raw_precip, events_table, ynew='Upper (90%)')
    return events_table


def scipy_interp(raw_precip: pd.DataFrame, df: pd.DataFrame, 
                                ynew: str='Expected Value') -> pd.DataFrame:
    '''Interpolates the ynew values for the passed df given the Log10_ARI 
       and ynew valuea contained within the raw_precip dataframe.
    '''
    f = interpolate.interp1d(raw_precip['Log10_ARI'],np.log(raw_precip[ynew]))
    df[ynew] =np.exp(f(df['Log10_ARI']))
    return df


def find_optimal_curve_std(df: pd.DataFrame, lower: str=r'Lower (90%)', 
            upper: str=r'Upper (90%)', sdev: float=0.15) -> pd.DataFrame:
    '''Calculates/optimizes the standard deviation of the lognormal 
       distribution using the expected value, lower confidence limit/value,
       and the upper confidence limit/value. The sum of the squared residuals
       of the lower and upper confidence limits/values is used as the test 
       statistic (this statistic is minimized). Note that the sdev is the 
       initial estimate of the standard deviation. The fitted values should
       be compared to the lower and upper confidence limits/values to 
       validate the optimization. 
    '''
    df = df.copy()
    for i, val in enumerate(df.index):
        x = np.array([df.iloc[i][lower], df.iloc[i][upper], sdev, 
                                            df.iloc[i]['Expected Value']])
        def objective_find_std(x: np.ndarray) -> float:
            '''Calculates the sum of the squared residuals for the lower 
               and upper 90% confidence limits given the standard deviation 
               and expected value of the lognormal distribution. 
            '''
            return np.square(stats.lognorm(x[2],scale=x[3]).ppf(0.1)-
                x[0])+np.square(stats.lognorm(x[2],scale=x[3]).ppf(0.9)-x[1])
        bounds = ((df.iloc[i][lower], df.iloc[i][lower]), 
            (df.iloc[i][upper], df.iloc[i][upper]), (0, None), 
                (df.iloc[i]['Expected Value'], df.iloc[i]['Expected Value']))        
        solution = minimize(objective_find_std, x, method='SLSQP', 
                                                            bounds=bounds)
        final_st_d = solution.x[2]
        df.loc[val, 'Sigma'] = final_st_d
        df.loc[val, r'Fitted {0} Limit'.format(lower)] = stats.lognorm(
                                            final_st_d, scale=x[3]).ppf(0.1)
        df.loc[val, r'Fitted {0} Limit'.format(upper)] = stats.lognorm(
                                            final_st_d, scale=x[3]).ppf(0.9)
    return df


def find_optimal_curve_beta_dist_S(df: pd.DataFrame, alpha: float=2.0,
          beta: float=10.0, CN_min: int=10, Delta_CN: int=20) -> pd.DataFrame:
    '''Calculates/optimizes the parameters (alpha and beta) of the beta 
       distribution representing the potential retention, S, normalized by an
       upper limit of the maximum potential retention, Smax, i.e. S/Smax. The
       upper limit, Smax, is based on a CN = 10, which is a reasonable lower 
       limit based on the TR-55 and Ponce 1996 "Runoff Curve Number: Has It 
       Reached Maturity?". Note that S=(1000/CN)-10. The fit is based on the
       respective CN value for the antecedent moisture conditions (AMC) I, II,
       and III. For each respective CN AMC, the corresponding potential 
       retention, S, respresents the 10% quantile, model value, and 90% 
       quantile of the beta distribution. The sum of the squared residuals of
       the 10% quantile, 90% quantile and modal value is used as the test 
       statistic (this statistic is minimized). Note that the alpha and beta 
       represent initial estimates. The fitted values should be compared to 
       the lower and upper confidence limits/values to validate the 
       optimization.
    '''
    df = df.copy()                                                                                
    '''max difference between 10% quantile CN and lower limit'''
    for i, val in enumerate(df.index):
        x = np.array([((1000/df.iloc[i]['AMC I (Dry)'])-10), 
                                          ((1000/df.iloc[i]['AMC II'])-10), 
                        ((1000/df.iloc[i]['AMC III (Wet)'])-10), alpha, beta, 
                                      ((1000/df.iloc[i]['AMC I (Dry)'])-10)])
        def objective_find_std(x: np.ndarray) -> float:
            '''Calculates the sum of the squared residuals for ACM I, AMC II,
               and AMC III curve numbers given the alpha and beta parameters
               of the beta distribution and Smax. 
            '''
            return np.square(x[5]*stats.beta(x[3], x[4]).ppf(0.9)-                                   
              x[0])+np.square(x[5]*(x[3]-1)/(x[3]+x[4]-2)-
              x[1])+np.square(x[5]*stats.beta(x[3], x[4]).ppf(0.1)-x[2])


        bounds = ((x[0], x[0]), (x[1], x[1]), (x[2], x[2]), (1.0, None), 
                                                              (1.0, None), 
                (x[0], 1000/(max(df.iloc[0]['AMC I (Dry)']-Delta_CN, 1))-10))  

        solution = minimize(objective_find_std, x, method='SLSQP', 
                                                            bounds=bounds)
        final_alpha = solution.x[3]
        final_beta = solution.x[4]
        final_S_limit = solution.x[5]
        df.loc[val, 'alpha'] = final_alpha
        df.loc[val, 'beta'] = final_beta
        df.loc[val, 'CN Lower Limit'] = 1000/(final_S_limit+10)
        df.loc[val, r'Fitted AMC I (Dry)'] = 1000/(10+final_S_limit*stats.beta(
                                            final_alpha, final_beta).ppf(0.9))
        df.loc[val, r'Fitted AMC II'] = 1000/(10+final_S_limit*((final_alpha-
                                                1)/(final_alpha+final_beta-2)))
        df.loc[val, r'Fitted AMC III (Wet)'] = 1000/(10+final_S_limit*stats.beta(
                                            final_alpha, final_beta).ppf(0.1))
    return df


def RandomizeData(df: pd.DataFrame, number: int, outputs_dir: str, 
  filename: str, dur: int=24, seed: int=None, variable: str='Precipitation',
                   plot: bool=False, display_print: bool=True) -> pd.DataFrame:
    '''Randomly selects a precipitation amount from the log-normal 
       distribution given the expected value and optimized standard devation 
       or a curve number from the beta distribution given the AMC II value and
       the optimized parameters for each recurrance interval/event.
    '''
    assert variable == 'Precipitation' or variable == 'CN', ("Check variable,"
                                        "currently only precipitation or CN"
                                            "available. No results computed")
    df = df.copy()
    if not seed:
        seed = np.random.randint(low = 0, high = 10000)
    np.random.seed(seed)
    current_col = 'Random {}'.format(variable)
    if variable == 'CN':
        df_filled = pd.DataFrame(index = np.arange(1, number+1, 1))
        df = df_filled.join(df, how = 'outer')
        df = df.fillna(method='ffill')
        S_limit = 1000/df.iloc[0]['CN Lower Limit']-10
        STable = S_limit*np.random.beta(df['alpha'], df['beta'], size=number)
        CNTable = 1000/(STable+10)
        df[current_col] = CNTable
        df[current_col] = df[current_col].apply(lambda x: int(x))
        df_rename = df.copy()
        df_rename.index.name ='E'
    if variable=='Precipitation': 
        df[current_col] = np.random.lognormal(np.log(df['Expected Value']), 
                                                    df['Sigma'], size=number)
        idx = df[df[current_col] < df['Lower (90%)']].index 
        df.loc[idx, current_col] = df.loc[idx, 'Lower (90%)']
        idx = df[df[current_col] > df['Upper (90%)']].index
        df.loc[idx, current_col] = df.loc[idx, 'Upper (90%)']
        df_rename = df.copy()
        df_rename.index.name ='Tr'
    rand_data = [col for col in df.columns.tolist() if 'Random' in col]
    if os.path.isdir(outputs_dir) == False:
        os.mkdir(outputs_dir)
    df_rename.to_csv(outputs_dir/filename) 
    if display_print: 
            print('{0} - Seed:'.format(variable), seed) 
            if variable == 'CN':
              print(display(df[rand_data].head(2)))    
    if plot: plot_rand_precip_data(df, rand_data, dur)
    return df[rand_data]


def join_rdata_tables(rdata_tables: list, type: str, 
                                display_print: bool=True) -> pd.DataFrame:
    '''Concatenates the dataframe elements of the passed list producing a 
       single dataframe. This resulting dataframe's index is set from 1 to 
       the length of the dataframe.
    '''
    rdata_table1 = rdata_tables[0]
    rdata_table2 = rdata_tables[1]
    rdata_table3 = rdata_tables[2]
    rdata_table4 = rdata_tables[3]
    rdata_table1 = rdata_table1.reset_index()
    rdata_table2 = rdata_table2.reset_index()
    rdata_table3 = rdata_table3.reset_index()
    rdata_table4 = rdata_table4.reset_index() 
    rdata_table = pd.concat([rdata_table1, rdata_table2, rdata_table3, 
                                                            rdata_table4])
    rdata_table=rdata_table.rename(columns={'index':'Tr'})
    nrows=rdata_table.shape[0]
    df=rdata_table.set_index(np.arange(1, nrows+1), drop=True)
    if type=='Precip' and display_print: 
        print('{} Randomly Selected Events > 2 year RI'.format(nrows))
    if display_print: display(df.head(2))
    return df


def get_quartiles(raw_temporals: pd.DataFrame, dur: int, qrank: list, 
                    qmap: dict, vol: int, reg: int, plot: bool=False) -> dict:
    '''For each quantile, extract the temporal data from the raw_temporals 
       dataframe, convert the data to numeric, store the data in a dictionary, 
       and plot the deciles.
    '''
    idx_name = raw_temporals.index.name
    assert idx_name in ['percent of duration', 'hours'], "Check temporal data"
    curve_group = {}
    for key in qmap['map'].keys():            
        q = raw_temporals[qmap['map'][str(key)][0]:qmap['map'][str(key)][1]].copy()
        if idx_name == 'percent of duration':
            q.index.name = None
            q = q.T
            tstep = dur/(q.shape[0]-1)
            q['hours'] = np.arange(0, dur+tstep, tstep)
        elif idx_name == 'hours':
            q = q.reset_index()
            q['hours'] = pd.to_numeric(q['hours'])
        q = q.set_index('hours')  
        for col in q.columns:
            q[col] = pd.to_numeric(q[col])
        curve_group[key] = q                
    if plot: plot_deciles_by_quartile(curve_group, qrank, qmap, vol, reg, dur)
    return curve_group


def map_quartiles_deciles(n_samples: int=75, seed: int=None, 
                plot: bool=False, display_print: bool=True) -> pd.DataFrame:
    '''Constructs a dataframe containing randomly selected deciles for the 
       specified number of samples (events).
    '''
    if not seed:
        seed = np.random.randint(low=0, high=10000)
    np.random.seed(seed)
    df = pd.DataFrame(index=np.arange(1, n_samples+1))
    df['Deciles'] = np.random.randint(1, 10, n_samples)*10
    if plot: plot_decile_histogram(df)
    if display_print: print('Seed - Deciles:', seed)
    return df


def prep_cn_table(CN: int, arc_data: dict) -> pd.DataFrame:
    '''Constructs a dataframe with the AMC II curve number (CN), the dry/lower
       CN (AMC I), and the wet/upper CN (AMC III). The AMC I, AMC II, and AMC 
       III curve numbers refer to different antecedent soil moisture 
       conditions governing runoff, which were obtained from 
       NEH Part 630, Chapter 10, Table 10-1
       (https://www.wcc.nrcs.usda.gov/ftpref/wntsc/H&H/NEHhydrology/ch10.pdf)
    '''
    dic = {'AMC I (Dry)': arc_data['Dry'], 'AMC II': CN, 
                                            'AMC III (Wet)': arc_data['Wet']}
    df = pd.DataFrame(dic, index = [1])
    return df


def populate_event_precip_data(random_cns: pd.DataFrame, 
    temporals: pd.DataFrame, random_precip_table: pd.DataFrame,
    data_table: pd.DataFrame, curve_group: dict, dur: int=24,
    adjust_CN_less24: bool = False) -> (pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame):
    '''Calculates cumulative and incremental runoff for each event using a 
       randomly selected precipitation amount, quartile specific temporal 
       distribution, and curve number. 
    '''
    precip_data = random_precip_table.copy()
    events_log = {}
    runids = []
    simID = int(str(dur)+'0000')
    output_precip_data = pd.DataFrame(index = curve_group['q1'].index) 
    cum_excess = pd.DataFrame(index = curve_group['q1'].index)
    incr_excess = pd.DataFrame(index = curve_group['q1'].index)
    for event in precip_data.index:
        simID +=1
        precip = precip_data.loc[event, 'Random Precipitation']
        orig_q =  precip_data.loc[event, 'Quartile']
        return_pd = precip_data.loc[event, 'Tr']
        cn = random_cns.loc[event, 'Random CN']
        decile = temporals.loc[event, 'Deciles']
        t_curve=curve_group['q{}'.format(int(orig_q))]['{}%'.format(decile)]
        rand_rain = t_curve*precip/100
        if dur < 24 and adjust_CN_less24:
            adj_CN, s, ia = update_CN(cn, dur, precip)
        else:
            s  = S_24hr(cn)
            ia = IA_24hr(s)
        excess = rand_rain.apply(calculate_excess, args=(ia, s))
        runid='E{}_'.format(event)+'{}Hr_'.format(dur)+'Q{}_'.format(int(orig_q))+'D{}_'.format(decile)+'CN{}'.format(cn)
        sim_ID = 'E{}'.format(simID) 
        events_log[sim_ID] = runid 
        output_precip_data[sim_ID] = rand_rain
        cum_excess[sim_ID] = excess
        incr_excess[sim_ID] = adjust_incremental(rand_rain, excess)
    return output_precip_data, cum_excess, incr_excess, events_log


def calc_excess_rainfall(eventID: dict, precip: dict, 
                                        random_cns: pd.DataFrame, idur: int,  
                                    adjust_CN_less24: bool=False) -> list:
    '''Calculates cumulative and incremental runoff for each event using a 
       randomly selected precipitation amount, quartile specific temporal 
       distribution, and curve number. This function performs the same 
       calculations as populate_event_precip_data but is employed by 
       the distalEventsTable.ipynb.
    '''
    count = 0
    for k, v in eventID.items():
        if count == 0:
            idx = [float(i) for i in list(precip[v].keys())]
            final_precip = pd.DataFrame(index = idx)
            cum_excess = pd.DataFrame(index = idx)
            incr_excess = pd.DataFrame(index = idx)
            count+=1
        excess = []
        cum_precip = list(precip[v].values())
        total_precip = cum_precip[-1]
        cn = random_cns.loc[int(k)]['Random CN']
        if idur < 24 and adjust_CN_less24:
            adj_cn, s, ia = update_CN(cn, idur, total_precip)
        else:
            s  = S_24hr(cn)
            ia = IA_24hr(s)
        for p in cum_precip:
            excess.append(calculate_excess(p, ia, s))
        cum_excess[v] = excess
        final_precip[v] = cum_precip
        incr_excess[v] = adjust_incremental(final_precip[v], cum_excess[v])
    results = [cum_excess, final_precip, incr_excess] 
    return results


def update_CN(CN: int, duration: int, 
                        grid_avg_precip: float) -> (int, float, float):
    '''Adjusts the curve number (CN), potential maximum retention after 
       runoff begins (S), and intial abstraction (Ia) for durations less than
       24 hours. Contact Kaveh Zomorodi: kzomorodi@Dewberry.com for 
       additional details regarding the adj_CN equation.
    '''
    s24                = S_24hr(CN)
    ia24               = IA_24hr(s24)
    qcn_24             = QCN_24hr(grid_avg_precip, s24)
    losses_24hr        = infiltration_24hr(grid_avg_precip, s24, qcn_24)
    loss_rate          = losses_24hr/24
    duration_loss_rate = loss_rate*duration
    loss_plus_ia       = duration_loss_rate + ia24
    runoff             = grid_avg_precip - loss_plus_ia
    adj_CN = 1000/((5*(grid_avg_precip+2*runoff-(4*runoff**2+5*
                                        grid_avg_precip*runoff)**0.5))+10)
    adj_s = S_24hr(adj_CN)
    adj_ia = IA_24hr(adj_s)
    return int(adj_CN), adj_s, adj_ia


def S_24hr(CN: int) -> float:
    '''Calculates the potential maximum retention after runoff begins (S), in 
       inches.
    '''
    return (1000-10*CN)/CN


def IA_24hr(s24: float) -> float:
    '''Calculats the inital abstraction (Ia) as a function of the maximum
       potentail rention (S). Lim et al. (2006) suggest that a 5% ratio of 
       Ia to S is more appropriate for urbanized areas instead of the more 
       commonly used 20% ratio 
       (https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1752-1688.2006.tb04481.x).
    '''
    return 0.2*s24


def QCN_24hr(grid_avg_precip: float, s24: float) -> float:
    '''Calculates runoff using equation 10-11 of NEH Part 630, Chapter 10
       (https://www.wcc.nrcs.usda.gov/ftpref/wntsc/H&H/NEHhydrology/ch10.pdf).
    '''
    return np.square(grid_avg_precip-0.2*s24)/(grid_avg_precip+0.8*s24)


def infiltration_24hr(grid_avg_precip: float, s24: float, 
                                                    qcn_24: float) -> float:
    '''Calculates the actual retention (or infilitration) after runoff 
       begins, in inches using equation 10-7 of NEH Part 630, Chapter 10
       (https://www.wcc.nrcs.usda.gov/ftpref/wntsc/H&H/NEHhydrology/ch10.pdf).
    '''
    return grid_avg_precip-0.2*s24-qcn_24


def calculate_excess(precip: float, ia: float, s: float) -> float:
    '''Calculates runoff using the curve number approach. See equation 10-9
       of NEH 630, Chapter 10
       (https://www.wcc.nrcs.usda.gov/ftpref/wntsc/H&H/NEHhydrology/ch10.pdf) 
    '''
    if precip <= ia:
        excess = 0
    else:
        excess = (np.square(precip-ia))/(precip-ia+s)
    return excess


def adjust_incremental(raw: pd.Series, excess: pd.Series) -> pd.Series:
    '''Calculates the incremental runoff depth (depth/timestep) using the
       cumulative_to_incremental function, and then redistributes the first
       non-zero incremental runoff value over the prior timesteps using the 
       incremental precipitation as a weighting function.
    '''
    raw_incremental = cumulative_to_incremental(raw)
    excess_incremental = cumulative_to_incremental(excess)
    idx0 = excess_incremental.index[0] 
    if excess_incremental.max() > 0:
        idx = excess_incremental[excess_incremental > 0].index[0] 
        weights = raw_incremental.loc[idx0:idx]/raw_incremental.loc[idx0:idx].sum()
        corrected_incremental = excess_incremental.copy() 
        corrected_incremental[idx0:idx] = weights*excess_incremental[idx]
    else:
        corrected_incremental = excess_incremental.copy()
    return corrected_incremental


def cumulative_to_incremental(vector: pd.Series) -> pd.Series:
    '''Converts the cumulative depth (precipitation or runoff) into the 
       incremental depth, i.e. the depth/timestep (rate).
    '''
    incremental_vector=[]
    cumsum=0
    for i, idx in enumerate(vector.index):
        if vector.iloc[i] == 0:
            data_point=0
        elif i <= len(vector.index)-1:
            data_point = vector.iloc[i] - cumsum
        incremental_vector.append(data_point)
        cumsum += data_point
    return pd.Series(incremental_vector, index=vector.index)


def convert_tempEpsilon(tempEpsilon: float, incr_excess: pd.DataFrame) -> int:
    '''Converts the tempEpsilon from the number of hours to the number of 
       corresponding timesteps.
    '''
    tstep = incr_excess.index[-1]/(incr_excess.shape[0]-1)
    adj_tempEpsilon = tempEpsilon/tstep
    if adj_tempEpsilon<1:
        warnings.warn("tempEpsilon less than the number of hours in a "
                            "timestep, adj_tempEpsilon set to 1 timestep")
        adj_tempEpsilon = 1
    elif not (adj_tempEpsilon).is_integer():
        warnings.warn("Number of timesteps not an integer, adj_tempEpsilon" 
                                    "rounded down to the closest integer")   
    adj_tempEpsilon = int(adj_tempEpsilon)
    return adj_tempEpsilon


def bin_sorting_dev(incr_excess: pd.DataFrame, nbins: int, 
                        min_thresh: float=0.01, display_print: bool = True, 
                                        display_plots: bool = True) -> list:
    '''Computes the histogram of the series data with the specified number
       of bins and returns the results as a list.
    '''
    runoff_data = incr_excess.sum()
    n_zero = len(runoff_data[runoff_data==0.0])
    n_blwthresh = len(runoff_data[runoff_data<min_thresh])
    runoff_data_non0 = runoff_data[runoff_data>=min_thresh]
    hist_data = np.histogram(runoff_data_non0, bins=nbins)
    bins = hist_data[1]
    binCount = hist_data[0]
    binData = dict(zip(binCount, bins))
    binData.pop(0, None)# Drop the last zero
    binData = sorted(binData.items(), key=operator.itemgetter(1))
    if n_zero > 0:
        n_blwthresh = n_blwthresh - n_zero + 1
    if n_blwthresh > 0: 
        binData = [(n_blwthresh, 0.0)]+binData
    h = [i[0] for i in binData]
    x = [i[1] for i in binData]    
    if display_print: print(display(binData))
    if display_plots:
        fig, axs = plt.subplots(1, 1)
        if len(binData)>=3:
            w = x[2]-x[1]
            axs.bar(x, h, w, align='edge') 
        else:
            axs.bar(x, h, align='edge') 
        axs.set_title('Histogram of the Binned Events')
        axs.set_xlabel('Incremental Excess (inches)')
        axs.set_ylabel('Number of Events')    
        plt.show()
    if max(h)>=1000.0:
        warnings.warn("One or more of the convolution bins has over 1000 "
            "nonzero events, which could take over 12 hours to complete, "
            "consider adjusting nbin and/or min_thresh in EventsTable.ipynb")
    return binData


def get_bin_slice(incr_excess: pd.DataFrame, binstart: float,
                                            binstop: float) -> pd.DataFrame:
    '''Slices the passed dataframe based on the events whose total runoff is
       bound by binstart and binstop.  
    '''
    incr_excess_sum = incr_excess.sum()
    usecols = incr_excess_sum[(binstart <= incr_excess_sum) 
                                            & (binstop > incr_excess_sum)] 
    usecols = list(usecols.index)
    dataslice = incr_excess[usecols]
    return dataslice


def prep_data_for_convolution(dataslice: pd.DataFrame, 
                                    adj_tempEpsilon: int) -> pd.DataFrame:
    '''The runoff for each column (event) in the passed dataframe is 
       calculated from zero to 24 hours for the intervals of length 
       tempEpsilon*timstep (30 minutes).
    '''
    curve_test_dict = {}
    for col in dataslice.columns:   
        curve_result = test_shapes(dataslice, col, adj_tempEpsilon) 
        curve_test_dict[col] = curve_result
    curve_test_df = pd.DataFrame.from_dict(curve_test_dict, orient='index').T
    curve_test_df_nanzero = curve_test_df.fillna(0)
    return curve_test_df_nanzero


def test_shapes(dataslice: pd.DataFrame, col: str, 
                                adj_tempEpsilon: int) -> np.ndarray[np.float64]:
    '''Calculates the total runoff for each interval, where the interval 
       width is equal to tempEpsilon times the timestep (30 minutes).
    '''
    df = dataslice.copy()
    y = list(df[col].values)+[0]
    curve_shape = []
    for i in range(1, len(y)-adj_tempEpsilon, adj_tempEpsilon):
        start = i
        stop = i+adj_tempEpsilon
        slice_sum = np.trapz(y[start:stop+1]) 
        curve_shape.append(slice_sum)
    curve_shape = np.array(curve_shape)
    return curve_shape


def conv_ts_zero_events(idx: list) -> list:
    """Creates a dictionary containing all possible combinations of the items 
       in the passed list as well as a list of ones whose length is equal to 
       the number of combinations. 
    """
    test_dic = {}
    for i, c in enumerate(idx):
        for nc in idx[i+1:]:
            test_dic[(c, nc)] = 1.0
    test_values = list(np.ones(len(test_dic.keys())))
    results = [test_dic, test_values]
    return results


def conv_ts(curve_test_df: pd.DataFrame, convEpsilon: float=150.0, 
                                volEpsilon: float=50.0, test_dic: dict=None, 
                                    test_values: list=None) -> (dict, list):
    '''For each event combination, a test statistic is calculated in order
       to quantify the similarity between the two temporal distributions.
       Note that in this function's code, "c" and "nc" refer to "column" 
       and "next column", respectively.
    '''
    df = curve_test_df.copy()
    if test_dic==None:
        test_dic = {}
    if test_values==None:
        test_values = []
    for i, c in enumerate(df.columns):
        for nc in df.columns[i+1:]:
            test = test_stat(df, df, c, nc, convEpsilon, volEpsilon)
            test_dic[(c, nc)] = test
    test_values += list(test_dic.values())
    test_values =  list(set(test_values))
    test_values.sort(reverse=True)
    return test_dic, test_values


def test_stat(c_df: pd.DataFrame, nc_df: pd.DataFrame, c: str, nc: str,
                            convEpsilon: float, volEpsilon: float) -> float:
    '''Calculates a test statistic that quantifies the similarity between 
       the two curves defined by "c" and "nc" within the passed dataframes.
       Note that in this function's code, "c" and "nc" refer to "column" 
       and "next column", respectively.
    '''
    dif = abs(c_df[c]-nc_df[nc])
    avg = (c_df[c]+nc_df[nc])/2.0
    if all(x==0.0 for x in avg):
        test = 1.0
    else:   
        perc_dif = dif/avg*100.0
        max_perc_dif = perc_dif.max() 
        total_dif = abs(c_df[c].sum()-nc_df[nc].sum())
        total_sum = c_df[c].sum()+nc_df[nc].sum()
        perc_dif_total = total_dif/(total_sum/2.0)*100.0     
        st1 = (convEpsilon-max_perc_dif)/convEpsilon
        st2 = (volEpsilon-perc_dif_total)/volEpsilon
        test = np.round(1 - np.sqrt((st1-1)**2+(st2-1)**2), 6)
    return test


def group_curves(test_dic: dict, test_values: list, events: list,
                                    test_stat_threshold: float=0.0) -> dict:
    '''If the test statistic for a particular pair of events is greater than
       the threshold and neither of the events are already in a group, add 
       them to a new group. Add all curves that are not a part of a group, 
       to their own group.
    '''
    curve_group = {}
    grouped = []
    g = 0
    for i, val in enumerate(test_values):
        if val>=test_stat_threshold:
            for k, v in test_dic.items():
                if v==val:
                    if k[0] not in grouped and k[1] not in grouped:
                        curve_group[g]=[k[0], k[1]]
                        grouped+=[k[0], k[1]]
                        g+=1
    to_group = [c for c in events if c not in grouped]
    for i in to_group:
        curve_group[g]=[i]
        g+=1
    return curve_group


def calc_mean_curves(curve_group: dict, 
                                    dataslice: pd.DataFrame) -> pd.DataFrame:
    '''Calculate the mean of the temporal distributions within each group.
    '''
    updated_curves = {}
    for k, v in curve_group.items():
        v_lst = extract_list(v)
        like_slice =  dataslice[v_lst] 
        mean_curve = like_slice.mean(axis=1)
        updated_curves[k] = mean_curve
    df_updated_curves = pd.DataFrame.from_dict(updated_curves)    
    return df_updated_curves


def check_upd_curv(all_groups: dict , updated_curves: pd.DataFrame, 
                    df: pd.DataFrame, convEpsilon: float, volEpsilon: float, 
                        test_stat_threshold: float) -> (dict, pd.DataFrame):
    '''The temporal distribution for each event within a group used to 
       calculate a mean temporal distribution is compared to that mean 
       temporal distribution using the same test statistic used to intially 
       combine the distributions into groups. If the test statistic for that 
       distribution is less than the test statistic threshold, the 
       distribution and its corresponding subgroup are removed from the 
       overall group used to calculate the mean curve. 
       The subgroup and remainder of the original group are assigned to new,
       separate groups. Once all distributions have been checked against 
       their mean distributions, the new groups are used to calculated 
       updated mean distributions. 
    '''
    updated_group = {}
    recalculate = False
    new_key = len(all_groups)
    for k, v in all_groups.items():
        test = []
        v_lst = extract_list(v)
        for i in v_lst:
            ts = test_stat(updated_curves, df, k, i, convEpsilon, volEpsilon)
            test.append(ts)
        if any(t < test_stat_threshold for t in test):
            failed = []
            v_update = v.copy()
            for j, ts in enumerate(test):
                if ts < test_stat_threshold:
                    failed.append(v_lst[j])
            num = 0
            for f in failed:
                for lst in v:
                    if f in lst and lst in v_update:
                        v_update.remove(lst)
                        if num == 0:
                            updated_group[k] = lst
                            num+=1
                        else:
                            updated_group[new_key] = lst
                            new_key+=1
            if len(v_update) > 0:               
                updated_group[new_key] = extract_list(v_update)
                new_key+=1
            recalculate = True
        else:
            updated_group[k] = v_lst
    if recalculate: updated_curves = calc_mean_curves(updated_group, df)
    return updated_group, updated_curves


def extract_list(nested_list: list) -> list:
    '''Extract all of the elements from the sublists within the list and 
       return the elements as a list.
    '''
    v_lst = nested_list
    while type(v_lst[0])==list:
        v_lst = [val for sublist in v_lst for val in sublist]
    return v_lst


def map_curve_groups(curve_group: dict, curve_group1: dict, 
                                            ungroup: bool = False) -> dict:
    '''Map the temporary event keys back to the orignal event IDs to keep a 
       record of events within each group.
    '''
    all_groups = {} 
    for k, v in curve_group1.items():
        temp_list = []
        for i in v:
            if ungroup:
                temp_list+=curve_group[i]
            else:
                temp_list.append(curve_group[i])
        all_groups[k] = temp_list 
    return all_groups


def renumber_dic_keys(updated_group: dict, group_start_num: int) -> dict:
    '''Renumber the dictionary keys so that they are ascending.
    '''
    keys = sorted(updated_group.keys())
    new_dic = {}
    for k in keys:
        new_dic[k+group_start_num] = updated_group[k]
    return new_dic   


def final_test_stat(updated_group: dict, updated_curves: pd.DataFrame, 
            df: pd.DataFrame, convEpsilon: float, volEpsilon: float) -> dict:
    '''For each group of distributions, the test statistic for each temporal
       distribution and corresponding mean temporal distribution (the group 
       average) is calculated.
    '''
    test_results = {}
    for k, v in updated_group.items():
        test = []
        for i in v:
            ts = test_stat(updated_curves, df, k, i, convEpsilon, volEpsilon)
            test.append(ts)
        test_results[k] = test
    return test_results


def dic_to_list(dic: dict, get_set: bool=False) -> list:
    '''Extracts the values from each key within a dictionary and returns the 
       values as a single list.
    '''
    lst_lst = list(dic.values())
    single_lst = list([val for sublist in lst_lst for val in sublist])
    if get_set:
        single_lst =  set(single_lst)
    return single_lst


def adj_duration_weight(dur_weight: float, lower_limit: int, 
                                        display_print: bool=True) -> float:
    '''Adjust the duration weight by the lower recurrance interval since 
       the events themseleves are truncated by this lower value.
    '''
    adj_dur_weight = dur_weight*1.0/lower_limit
    if display_print: print(adj_dur_weight)
    return adj_dur_weight


def Calc_Group_Weight(final_groups: dict, duration_weight: float,
                                        display_print: bool = True) -> dict:
    '''Calculates the weight of each group of curves, such that the sum of 
       all the weights adds to the duration_weight.
    '''
    n_curves = {}
    weight_curves = {}
    for k in final_groups.keys():
        n_curves[k] = len(final_groups[k])
    n_curves_tot = float(sum(n_curves.values()))   
    for k in n_curves.keys():
        weight_curves[k] = (n_curves[k]/n_curves_tot)*duration_weight
    total_weight = sum(weight_curves.values())
    print('Sum of weights: {}'.format(total_weight))
    return weight_curves


def Rename_Final_Groups(curve_weight: dict, dur: int) -> dict:
    '''Sorts the groups by their weight and then renames the groups so that
       the group with the largest weight is designed E0001 and the group with
       the next largest weight is designated E0002 (for the 6 hour duration). 
       The thounsands place is set to 0, 1, 2, 3 for the 6, 12, 24, and 96 
       hour durations, respectively. A dictionary mapping the original group
       names to the new group names is returned. 
    '''
    assert dur in [6, 12, 24, 96], "Naming convention not set for duration"
    rename_map = {}
    weights = sorted(list(set(curve_weight.values())), reverse=True)
    dur_adj = {6:0, 12:1, 24:2, 96:3 }
    num = 1
    for i in weights:
        for k, v in curve_weight.items():
            if i==v:
                ID = 'E{0}{1}'.format(dur_adj[dur],str(num).zfill(3))
                rename_map[k] = ID 
                num+=1
    return rename_map    


def determine_tstep_units(incr_excess: pd.DataFrame) -> dict:
    '''Determines the timestep and the timestep's units of the incremental
       excess runoff.
    '''
    assert incr_excess.index.name == 'hours', 'Timestep and timesteps units' 
    'cannot be calculated if the runoff duration is not in units of hours'
    tstep = incr_excess.index[-1]/(incr_excess.shape[0]-1)
    dic = {}
    if tstep < 1.0:        
        dic[int(60.0*tstep)] = 'MIN' 
    elif tstep >= 1.0:
        dic[int(tstep)] = 'HOUR'
    else:
        print('Timestep and timestep units were not determined')
    return dic


def dss_map(outputs_dir: str, var: str, tstep: int, tstep_units: str,
                units: str, dtype: str='INST-VAL', IMP: str='DSS_MAP.input', 
                        to_dss: str='ToDSS.input', open_op: str='w') -> None:
    '''Creates a map file containing the data structure for DSSUTL.EXE.
    '''
    var8 = var[:8]
    ts = '{0}{1}'.format(tstep, tstep_units)
    output_file = outputs_dir/IMP
    datastring = "EV {0}=///{0}//{1}// UNITS={2} TYPE={3}\nEF [APART] [BPART] [DATE] [TIME] [{0}]\nIMP {4}\n".format(var8, ts, units, dtype, to_dss)
    with open(output_file, open_op) as f: 
        f.write(datastring)
    return None


def excess_df_to_input(outputs_dir: str, df: pd.DataFrame, tstep: float,
                        tstep_units: str, scen_name: str, open_op: str='w', 
                                        to_dss: str='ToDSS.input') -> None:
    '''Writes the excess rainfall dataframe to an input file according to the 
       struture specified within DSS_MAP.input.
    '''
    temp_data_file = outputs_dir/to_dss
    cols = df.columns.tolist()
    start_date = datetime(2000, 5, 1, 00, 00)
    with open(temp_data_file, open_op) as f:
        for i, col in enumerate(cols):
            m_dtm = start_date
            event_data = df[col]
            for j, idx in enumerate(event_data.index):
                if j > 0: 
                    if tstep_units == 'MIN': 
                        m_dtm+=pd.Timedelta(minutes = tstep)
                    elif tstep_units == 'HOUR': 
                        m_dtm+=pd.Timedelta(hours = tstep)
                htime_string = datetime.strftime(m_dtm, '%d%b%Y %H%M')
                runoff = event_data.loc[idx]
                f.write('"{}"'.format(scen_name)+' '+col+' '+htime_string+' '+str(runoff)+'\n')
    return None


def make_dss_file(outputs_dir: str, bin_dir: str, dss_filename: str,
                        dssutil: str='DSSUTL.EXE', IMP: str='DSS_MAP.input', 
                    to_dss: str='ToDSS.input', remove_temp_files: bool=True, 
                                        display_print: bool = True) -> None:
    '''Runs the DSSUTL executable using the DSS_MAP.input file to map the 
       excess rainfall data from the ToDSS.input file and saves the results
       to a dss file.
    '''
    cwd = os.getcwd()
    shutil.copy(bin_dir/dssutil, outputs_dir)
    os.chdir(outputs_dir)
    os.system("{0} {1}.dss INPUT={2}".format(dssutil, dss_filename, IMP))
    time.sleep(5)
    if remove_temp_files:
        os.remove(outputs_dir/IMP)
        os.remove(outputs_dir/dssutil)
        os.remove(outputs_dir/to_dss)
    filepath = outputs_dir/dss_filename
    os.chdir(cwd)
    if display_print: print('Dss File written to {0}.dss'.format(filepath))
    return None


def dic_key_to_str(orig_dic: dict) -> dict:
    '''Converts the keys of the passed dictionary to strings.
    '''
    dic_str = {}
    for k in orig_dic.keys():
        dic_str[str(k)]=orig_dic[k]
    return dic_str


def extract_event_metadata(outfiles: list, events_metadata: dict,  
                outputs_dir: str, remove_intermediates: bool = True) -> dict:
    '''Loads all of the intermediate metadata files created during the 
       randomization steps and saves them to a single dictionary.
    '''
    for f in outfiles:
        file = outputs_dir/f
        df = pd.read_csv(file)
        if remove_intermediates:
            os.remove(file)
        if 'Rand_Precip' in f:
            if 'Q1' in f: dfQ1 = df.copy()
            if 'Q2' in f: dfQ2 = df.copy()
            if 'Q3' in f: dfQ3 = df.copy()
            if 'Q4' in f: dfQ4 = df.copy()
        elif 'Rand_CN' in f:
            dfCN = df.set_index('E').copy()
    dfQ = pd.concat([dfQ1, dfQ2, dfQ3, dfQ4])
    dfQ['E'] = np.arange(1, len(dfQ)+1)
    dfQ = dfQ.set_index('E')
    new_col = []
    for col in list(dfCN.columns):
        if 'CN' not in col:
            new_col.append(col+' CN')
        else:
            new_col.append(col)   
    dfCN.columns = new_col
    dfcomb = dfQ.join(dfCN, on = 'E')
    dic = {}
    for k, v in events_metadata.items():
        data = v.split('_')
        E = int(data[0].replace('E', ''))
        dic[E] = {'EventID': k, 'Decile': int(data[3].replace('D', ''))}
    dic_df = pd.DataFrame.from_dict(dic).T
    dic_df.index.name = 'E'
    metadata = dfcomb.join(dic_df, on = 'E').to_dict()
    return metadata


def checkif_SWinfra(pluvial_params_dir: plib, BCN: str, 
                                            display_print: bool=True) -> str:
    '''Check the pluvial parameters Excel Workbook to determine if the 
       specified pluvial domain has stormwater infrastructure.
    '''
    df = pd.read_excel(pluvial_params_dir, sheet_name = 'Pluvial_Domain')
    pp = df[df['Pluvial Domain']==BCN]
    run_reduced = pp['SW Infrastructure (YES or NO)'].values[0]
    if display_print: 
        print('Is there stormwater infrastructure? ->', run_reduced)
    return run_reduced


def get_stormwater_rate_cap(pluvial_params_dir: plib, BCN: str, 
                        SW_rate_col: str, SW_cap_col: str, SW_eff_col: str,
                                        display_print: bool=True) -> list:
    '''Extract the stormwater removal rate, capacity, and efficiency from the
       pluvial parameters Excel Workbook for the specified boundary condition 
       name. 
    '''
    df = pd.read_excel(pluvial_params_dir, sheet_name = 'Pluvial_Domain')
    pp = df[df['Pluvial Domain']==BCN]
    columns = list(pp.columns)
    assert SW_rate_col and SW_cap_col in columns, ('The specified rate and/or'
        ' capacity column names are not in the Pluvial_Parameters.xlsx. Update'
        ' the column names and rerun.')
    rate = pp[SW_rate_col].values[0]
    maxcap = pp[SW_cap_col].values[0]
    if SW_eff_col not in columns:
        warnings.warn('The provided stormwater efficiency column is not in the'
            ' Pluvial_Parameters.xlsx. An effiency of 100 percent will be used'
            ' unless the Workbook is updated.')
        eff = 1.0
    else:
        eff = pp[SW_eff_col].values[0]
    assert 0.0<=eff<=1.0, ('Check that the specified stormwater efficiency'
                        ' is between 0 and 1, i.e. between 0 and 100 percent')
    rate_cap_eff = [rate, maxcap, eff]
    if display_print: 
        print(display(pp.head(2)))
        print('SW Rate: {0} in/30min\nSW Capacity: {1} in/unit area\nSW '
                                'Efficiency: {2} percent'.format(rate, maxcap,
                                                                    eff*100.0))
    return rate_cap_eff



def adj_stormwater_rate_cap(rate: float, maxcap: float, efficiency: float, 
                                                verbose: bool=True) -> list:
    """Adjust the stormwater rate and capacity by the stormwater efficiency.
    """
    adj_rate = rate*efficiency
    adj_cap = maxcap*efficiency
    results = [adj_rate, adj_cap]
    if verbose:
        print('Adjusted SW Rate: {0} in/30min\nAdjusted SW Capacity: {1} '
                                'in/unit area'.format(adj_rate, adj_cap))
    return results


def determine_timestep(dic_dur: dict, display_print: bool=True) -> float:
    '''Calculates the timestep of the rainfall excess data contained within
       the passed dictionary.
    '''
    time_ord = dic_dur['time_idx_ordinate']
    assert time_ord == 'Hours', 'Timestep is not in units of hours'
    time_idx = dic_dur['time_idx']
    timestep = float(time_idx[-1])/(len(time_idx)-1)
    if display_print: print('Time Step: {0} {1}'.format(timestep, time_ord))
    return timestep


def storm_water_simulator(minrate30: float, maxrate30: float, ts: float, 
                        seed: int=None, display_print: bool=True) -> list:
    '''Randomly selects a stormwater removal rate between the minimum and
       maximum values and then calculates the maximum stormwater capacity.
    '''
    if not seed:
        seed = np.random.randint(low=0, high=10000)
    np.random.seed(seed)
    adj_rate = np.random.uniform(minrate30, maxrate30)   
    maximum_capacity = adj_rate*(24.0/ts)
    results = [adj_rate, maximum_capacity, seed]
    if display_print: 
        print('Rate:', adj_rate, 'Maximum Capacity:', maximum_capacity, 'Seed:', seed)
    return results 


def reduced_excess(event: list, adj_rate: float, max_capacity: float) -> list:
    '''Calculates the reduced excess rainfall for the passed event using the
       adjusted stormwater removal rate and the maximum stormwater capacity.
    '''
    reduced_event = event.copy()
    remaining_cap = max_capacity
    for i, val in enumerate(event):
        if remaining_cap > 0: 
            remainder = val - adj_rate
            if remainder <= 0.0: 
                remainder = 0.0
                remaining_cap -= val
            if remainder > 0.0:
                remaining_cap -= adj_rate   
            if remaining_cap >=0.0:
                reduced_event[i] = remainder
            if remaining_cap < 0.0:
                reduced_event[i] = (remainder-remaining_cap)
                remaining_cap = 0.0
                continue
        elif remaining_cap <=0:
            reduced_event[i] = val  
    int_total = np.round(sum(event), 5)
    f_total = np.round(sum(reduced_event)+max_capacity-remaining_cap, 5) 
    assert int_total == f_total, 'Check reduced runoff calculation, mass not conserved'
    return reduced_event


def calc_lateral_inflow_hydro(lid: pd.DataFrame, ReducedTable: dict, 
                            StormwaterTable: dict, durations: list, BCN: str, 
        									display_print: bool=True) -> dict:
    '''Calculate the lateral inflow hydrographs for each event and domain 
       given the lateral inflow contributing area.
    '''
    lid_names = list(lid['Lateral Inflow Domain'])
    if display_print: print('Lateral Inflow Domains:', lid_names)
    for dur in durations:
        ReducedTable[dur]['lateral_BC_units'] = 'cfs'
        ts = determine_timestep(StormwaterTable[dur], display_print=False)
        events_dic = StormwaterTable[dur]['BCName'][BCN]
        for l in lid_names:
            slicedf = lid[lid['Lateral Inflow Domain']==l]
            a_sqmile = slicedf['Lateral Inflow Area (miles^2)'].values[0]
            a_sqft = a_sqmile*5280.0**2
            li_dic = {}
            for k, v in events_dic.items():
                Q_per_ts = [(x/12.0)*a_sqft for x in v]
                li_dic[k] = [x/(ts*60.0*60.0) for x in Q_per_ts]
                ReducedTable[dur]['BCName'][l] = li_dic            
    return ReducedTable


def get_lateral_inflow_domains(pluvial_params_dir: plib, BCN: str, 
                                display_print: bool=True) -> pd.DataFrame:
    '''Load the pluvial parameters Excel Worksheet and extract the later 
       inflow domains corresponding with the specified boundary condition 
       name.
    '''
    df=pd.read_excel(pluvial_params_dir, sheet_name = 'Lateral_Inflow_Domain')
    lid = df[df['Pluvial Domain']==BCN].copy(deep=True)
    if display_print: print(display(lid.head(2)))
    return lid


def combine_results(var: str, outputs_dir: str, BCN: str, durations: list,
        tempEpsilon_dic: dict, convEpsilon_dic: dict, volEpsilon_dic: dict,
                run_dur_dic: dict=None, remove_ind_dur: bool = True) -> dict:
    '''Combines the excess rainfall *.csv files for each duration into a 
       single dictionary for all durations.
    '''
    assert var in ['Excess_Rainfall', 'Weights'], 'Cannot combine results'
    dic = {}
    df_lst = []
    for dur in durations:
        tE = tempEpsilon_dic[str(dur)]
        cE = convEpsilon_dic[str(dur)]
        vE = volEpsilon_dic[str(dur)]
        scen='{0}_Dur{1}_tempE{2}_convE{3}_volE{4}'.format(BCN, dur, tE, cE, vE)
        file = outputs_dir/'{}_{}.csv'.format(var, scen)
        df = pd.read_csv(file, index_col = 0)
        if var == 'Excess_Rainfall':
            df_dic = df.to_dict()
            dates = list(df.index)
            ordin = df.index.name.title()
            events = {}
            for k, v in df_dic.items():
                if 'E' in k:
                    events[k] = list(v.values())
            key ='H{0}'.format(str(dur).zfill(2))
            val = {'time_idx_ordinate': ordin, 
                   'run_duration_days': run_dur_dic[str(dur)],
                    'time_idx': dates, 
                    'pluvial_BC_units': 'inch/ts', 
                    'BCName': {BCN: events}}         
            dic[key] = val
        elif var == 'Weights':
            df_lst.append(df)
        if remove_ind_dur:
            os.remove(file)    
    if var == 'Weights':
        all_dfs = pd.concat(df_lst)
        weights_dic = all_dfs.to_dict()
        dic = {'BCName': {BCN: weights_dic['Weight']}}
        print('Total Weight:', all_dfs['Weight'].sum())
    return dic
    

def pad_pluvial_forcing(f_dic: dict, uniform_pad: bool = True, plen: int = 2,
                                                verbose: bool = True) -> dict:
    """Pad the time index and the pluvial forcing data of the passed pluvial 
       dictionary.
    """
    plen = int(plen)
    updated_dic = {}
    for d in list(f_dic.keys()):
        updated_dic[d] = {}
        for k, v in f_dic[d].items():
            run_dur = f_dic[d]['run_duration_days']
            run_dur_hr = run_dur*24.0
            idx = f_dic[d]['time_idx']
            tstep = idx[1] - idx[0]
            start = idx[-1] + tstep
            if not uniform_pad:
                plen = int((run_dur_hr-start)/tstep)+1
            if k == 'time_idx':
                updated_dic[d][k] = idx + list(np.arange(start, 
                                               start+plen*tstep, tstep))
            elif k == 'BCName':
                bcns = list(f_dic[d][k].keys())
                updated_dic[d][k] = {}
                for b in bcns:
                    updated_dic[d][k][b] = {}
                    for e, vals in f_dic[d][k][b].items():
                        updated_dic[d][k][b][e] = vals + list(np.zeros(plen))
            else:
                updated_dic[d][k] = v
        if verbose:
            print('Padded the forcing for {0} with {1} zeros'.format(d, plen))
    return updated_dic


def combine_metadata(outputs_dir: str, BCN: str, durations: list, 
        tempEpsilon_dic: dict, convEpsilon_dic: dict, volEpsilon_dic: dict,
                                        remove_ind_dur: bool = True) -> dict:
    '''Combines the metadata files for each duration into a single file for
       all durations.
    '''
    dic = {}
    for dur in durations:
        tE = tempEpsilon_dic[str(dur)]
        cE = convEpsilon_dic[str(dur)]
        vE = volEpsilon_dic[str(dur)]
        scen='{0}_Dur{1}_tempE{2}_convE{3}_volE{4}'.format(BCN, dur, tE, cE, vE)
        file = outputs_dir/'Metadata_{0}.json'.format(scen)  
        with open(file) as f:
            md =  json.load(f)
        key = 'H{0}'.format(str(dur).zfill(2))
        val = {'BCName': {BCN: md}}
        dic[key] = val
        if remove_ind_dur:
            os.remove(file)
    return dic   
    

def combine_distal_results(outfiles: list, outputs_dir: plib, var: str, 
        BCN: str, ordin: str='', pluvial_BC_units: str='', run_dur_dic: dict=None, 
                                        remove_ind_dur: bool=True) -> dict:
    '''Combines the excess rainfall results and metadata for each duration 
       into a single file for all durations.
    '''
    dic = {}
    for file in outfiles:
        if var == 'Excess' and 'Excess' in str(file):
            dur = int(str(file).split('_')[3].replace('Dur', ''))
            df = pd.read_csv(outputs_dir/file, index_col = 0)
            df_dic = df.to_dict()
            dates = list(df.index)
            events = {}
            for k, v in df_dic.items():
                if 'E' in k:
                    events[k] = list(v.values())
            val = {'time_idx_ordinate': ordin, 
                   'run_duration_days': run_dur_dic[str(dur)],
                   'time_idx': dates, 'pluvial_BC_units': pluvial_BC_units,
                   'BCName': {BCN: events}}  
        elif var=='Metadata' and 'Metadata' in str(file):
            dur = int(str(file).split('_')[2].replace('Dur', ''))
            with open(outputs_dir/file) as f:
                md =  json.load(f)
            val = {'BCName': {BCN: md}}
        else:
            continue        
        key ='H{0}'.format(str(dur).zfill(2))
        dic[key] = val
        if remove_ind_dur:
             os.remove(outputs_dir/file)
    return dic


def dict_to_df(dic: dict, display_head: bool=True) -> pd.DataFrame:
    '''Convert a dictionary of lists of non-equal length or individual
       floats/integers to a dataframe.
    '''
    count = 1
    for k, v in dic.items():
        if count==1:
            df = pd.DataFrame()
            if type(v)==int or type(v)==float:
                df[k] = [v]
            else:
                df[k] = v
            count+=1
        else:
            df1 = pd.DataFrame()
            if type(v)==int or type(v)==float:
                df1[k] = [v]
            else:
                df1[k] = v
            df = df.join(df1)
    if display_head: print(display(df.head()))    
    return df


#---------------------------------------------------------------------------#
# Plotting Functions
#---------------------------------------------------------------------------#

'''Plotting functions called by EventTable.ipynb. This 
   notebook calculates excess rainfall by first randomly selecting a 
   precipitation recurrance interval and corresponding precipitation amount, 
   precipitation temporal distribution, and curve number for the area of 
   interest. The randomly selected precipitation data and curve number are 
   then used by the curve number approach to calculate the excess rainfall 
   amount for the corresponding recurrance interval. The procedure is 
   repeated for the specified number of events/recurrance intervals. 
'''

#---------------------------------------------------------------------------#


def plot_area_of_interest(geo_df: geoDF, select_data: str, 
                                                column: str) -> plt.subplots:
    '''Plots the column of the geodataframe with matplotlib.
    '''
    fig = geo_df.plot(column = column, categorical = True, figsize = (10, 14))
    fig.set_title('Area of Interest (ID: {})'.format(select_data))
    fig.grid()


def plot_aoi_noaa_intersection(intersection_gdf: geoDF, 
                                            select_data: str) -> plt.subplots:
    '''Plots the intersection of the geodataframe and the NOAA Atlas 14 
       volumes and regions.
    '''
    intersection_gdf['Volume_Region'] = 'Volume: ' +\
                        intersection_gdf['Volume'].map(str) + ', Region: ' +\
                                        intersection_gdf['Region'].map(str)
    fig = intersection_gdf.plot(column='Volume_Region', categorical=True, 
                                                figsize=(10, 14), legend=True)
    fig.set_title('Area of Interest (ID: {}) by NOAA Atlas' 
                                                'Region'.format(select_data))
    fig.grid()



def plot_rand_precip_data(df: pd.DataFrame, rand_data: list, duration: int,
                                                xlim: int=2) -> plt.subplots:
    '''Plots the precipitation amount selected from the lognormal 
       distribution with the return period on the x-axis and the amount of 
       precipitation on the y-axis, along with the expected values, lower 
       90% confidence limits, and upper 90% confidence limits.
    '''
    fig, ax = plt.subplots(figsize=(20, 6))
    ax.grid(True, which="both")
    idx = list(df.index)
    ax.semilogx(idx, df['Upper (90%)'], color='darkolivegreen', 
                        linewidth=3, label=r'Upper (90%) Confidence Limit')
    ax.semilogx(idx, df['Expected Value'], color='darkred', linewidth=2, 
                                                    label='Expected Value')
    ax.semilogx(idx, df['Lower (90%)'], color='darkblue', linewidth=3, 
                                    label=r'Lower (90%) Confidence Limit')
    for col in rand_data:
        ax.scatter(idx, df[col], s=25, edgecolor='black', 
                    linewidth='1',  facecolor=np.random.rand(4,), label=col)
    def mil(x: float, pos: int) -> str:
        ''' Convert the passed x-value to a string.
        '''
        return '{}'.format(x)
    mil_formatter = FuncFormatter(mil)
    for axis in [ax.xaxis]:
        axis.set_major_formatter(mil_formatter)
    ax.set_xlabel('Return Period (years)', fontsize=18)
    ax.set_ylabel('P (inches)', fontsize=18)
    ax.legend()
    ax.set_xlim(xlim,)
    ax.set_ylim(0,)
    ax.set_title('Random Precipitation  \n{} Hour Duration'.format(duration),
                                                                fontsize=18)


def plot_deciles_by_quartile(curve_group: dict, qrank: list,
                qmap: dict, vol: int, reg: int, dur: int) -> plt.subplots:
    '''Plots the temporal distribution at each decile for each quartile. 
    '''
    fig, ax = plt.subplots(2,2, figsize=(24,10))
    for axi in ax.flat:
        axi.xaxis.set_major_locator(plt.MultipleLocator((
                                            curve_group['q1'].shape[0]-1)/6))
        axi.xaxis.set_minor_locator(plt.MultipleLocator(1))
    axis_num=[[0,0], [0,1], [1,0], [1,1]]
    for i, val in enumerate(qmap['map'].keys()):
        for col in curve_group[val].columns:
            plt.suptitle('Volume '+str(vol)+' Region '+str(reg)+' Duration '+\
                                str(dur), fontsize = 20, x  = 0.507, y = 1.02)
            ax[axis_num[i][0],axis_num[i][1]].plot(curve_group[val][col], 
                                                                label=col) 
            ax[axis_num[i][0],axis_num[i][1]].grid()
            ax[axis_num[i][0],axis_num[i][1]].set_title('Quartile {0}\n{1}%'
                    ' of Cases'.format(i+1, int(qrank[i]*100)), fontsize=16)
            ax[axis_num[i][0],axis_num[i][1]].legend(title='Deciles')
            ax[axis_num[i][0],axis_num[i][1]].set_xlabel('Time (hours)', 
                                                                fontsize=14)
            ax[axis_num[i][0],axis_num[i][1]].set_ylabel('Precip (% Total)', 
                                                                fontsize=14)
    plt.tight_layout()


def plot_decile_histogram(df: pd.DataFrame) -> plt.subplots:
    '''Plots a histogram of the randomly selected decile numbers within the
       passed dataframe.
    '''
    fig = df.hist(bins=20, figsize=(20,6), grid=False)


def plot_rainfall_and_excess(final_precip: pd.DataFrame, 
    cum_excess: pd.DataFrame, dur: int=24, iplot: bool=False) -> plt.subplot:
    '''Plots the cumulative rainfall and runoff for each randomly selected 
       event.
    '''
    fig, ax = plt.subplots(1, 2, figsize=(24,5))
    for axi in ax.flat:
        axi.xaxis.set_major_locator(plt.MultipleLocator(dur/6))
        axi.xaxis.set_minor_locator(plt.MultipleLocator(1))
    for col in final_precip.columns:
        ax[0].plot(final_precip[col])
        ax[1].plot(cum_excess[col]) 
    nevents = final_precip.shape[1]
    ax[0].set_title('{} Cumulative Rainfall' 
                                    'Events'.format(nevents), fontsize=18)
    ax[1].set_title('{} Cumulative Runoff' 
                                    'Events'.format(nevents), fontsize=18)
    ax[0].set_ylim(0, 1.1*final_precip.max().max())
    ax[1].set_ylim(0, 1.1*final_precip.max().max())
    ax[0].set_xlabel('Time (hours)', fontsize=18)
    ax[1].set_xlabel('Time (hours)', fontsize=18)
    ax[0].set_ylabel('Precip (inches)', fontsize=18)
    ax[1].set_ylabel('Runoff (inches)', fontsize=18)
    ax[0].grid(True)
    ax[1].grid(True)
    if iplot:
        plt.close(fig)
    return fig


def plot_curve_groups(reordered_group: dict, reordered_curves: pd.DataFrame,
                                    curve_test_df: pd.DataFrame, y_max: float, 
                                            final: bool=True) -> plt.subplots:
    '''Plots the mean temporal distribution and the corresponding 
       individual temporal distributions of a curve group for each group as 
       separate plots.  
    '''
    c_df = reordered_curves.copy()
    nc_df = curve_test_df.copy()
    x = list(c_df.index.values)
    if not final: x += [c_df.shape[0]]
    for c in c_df.columns:
        fig, ax = plt.subplots(figsize=(30,8))
        for nc in reordered_group[c]:
            y = nc_df[nc].values
            if not final: y = np.insert(y, 0, 0)
            ax.plot(x, y, alpha=0.75, label = nc);
        y = c_df[c].values
        if not final: y = np.insert(y, 0, 0)
        ax.plot(x, y, color='black', linewidth='2', label='Mean Curve')    
        ax.grid()
        ax.set_xlabel('Duration, [hours]')
        ax.set_ylabel('Runoff, [inches]')
        ax.set_ylim(0, y_max*1.1)
        ax.set_title('Group {} Temporal Distribution'.format(c))
        ax.legend()


def plot_grouped_curves(final_curves: dict, y_max: float, 
                                        iplot: bool=False) -> plt.subplots:
    '''Plots the mean curve of each group of curves determined using the 
       convolution test as well as the curves that were not grouped. 
    '''
    fig, ax = plt.subplots(figsize=(30,8))
    for col in final_curves.columns:
        ax.plot(final_curves[col]);
    ax.grid()
    ax.set_xlabel('Duration, [hours]')
    ax.set_ylabel('Runoff, [inches]')
    ax.set_ylim(0, y_max*1.1)
    ax.set_title('{} Temporal Curves'.format(final_curves.shape[1]))
    if iplot:
        plt.close(fig)
    return fig


def plot_reduced_excess(ReducedTable: dict, EventsTable: dict) -> plt.subplot:
    '''Plot the excess rainfall, reduced excess rainfall, and lateral inflow 
       hydrograph for the last event of each duration within the passed 
       dictionaries.
    '''
    for i, dur in enumerate(list(ReducedTable.keys())):
        fig, ax = plt.subplots(1, 1, figsize=(18, 6))
        idur = int(dur.replace('H', ''))   
        idx = ReducedTable[dur]['time_idx']
        tstep = idx[1] - idx[0]
        w = tstep/2
        lat_bounds = False
        for b in list(ReducedTable[dur]['BCName'].keys()):
            if 'D' in b:
                pbcn =  b
            elif 'L' in b and not lat_bounds:
                lbcn = b
                lat_bounds = True
        dic_ex = EventsTable[dur]['BCName'][pbcn]      
        dic_re = ReducedTable[dur]['BCName'][pbcn]
        k = list(dic_re.keys())[-1]
        ymax = max(dic_ex[k])
        ax.bar(idx, dic_ex[k], width = w, align = 'center', color = 'gray', 
        											label = 'Original Excess')        
        ax.bar(idx,dic_re[k], width = w, align = 'center', color = 'cyan', 
        											label = 'Reduced Excess')  
        ax.set_title('{0} Hour Duration (Event: {1})'.format(idur, k), 
        															size = 12)
        ax.set_xlabel('Time, [hours]')
        ax.set_xlim(left=0)
        ax.set_ylabel('Excess Rainfall, [inches]')
        ax.set_ylim(ymax*2.1, 0)
        ax.yaxis.set_major_formatter(FormatStrFormatter('%.2f'))    
        if lat_bounds: 
            dic_lat = ReducedTable[dur]['BCName'][lbcn]
            lih_units = ReducedTable[dur]['lateral_BC_units']
            ax2 = ax.twinx()  
            ymax2 = max(dic_lat[k])
            lns3 = ax2.plot(idx, dic_lat[k], 
            			label = '{0} Hydrograph'.format(lbcn), color = 'Navy')
            ax2.set_ylabel('Discharge, [{0}]'.format(lih_units))
            ax2.set_ylim(0, ymax2*2.1)
            lines, labels = ax.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax2.legend(lines + lines2, labels + labels2, loc='lower right')
        else:
            ax.legend(loc = 'lower right')


def plot_amount_vs_weight(weights_dic: dict, excess_dic: dict, mainBCN: str,
                                    distalBCN: str=None) -> plt.subplots:
    '''Plot the total excess rainfall for each event versus its weight.
    '''
    if distalBCN==None: distalBCN=mainBCN
    fig, ax = plt.subplots(1,1, figsize=(24,5))
    n = 0
    for dur in excess_dic.keys():
        weight = []
        runoff = []
        for k in excess_dic[dur]['BCName'][distalBCN].keys():
            n+=1
            runoff.append(sum(excess_dic[dur]['BCName'][distalBCN][k]))
            weight.append(weights_dic['BCName'][mainBCN][k])
        ax.plot(weight, runoff, linestyle = '', marker = '.', label = dur)
    ax.set_xlabel('Event Weight, [-]')
    ax.set_ylabel('Excess Rainfall, [inches]')
    ax.set_title('Excess Rainfall Amount Versus Event Weight '
                                                    '({} Events)'.format(n))
    ax.grid()
    ax.legend()   


def plot_tempEpsilons(events: pd.DataFrame, event_of_interest: str, 
                tempEpsilons: list, duration: int, verbose: bool=True) -> None:
    '''Plot the incremental excess rainfall curve for each tempEpsilon. 
    '''
    fig, ax = plt.subplots(figsize=(24, 5))
    ax.plot(events.index, events[event_of_interest], color='black',
                                             linewidth='1', label='Original') 
    for e in tempEpsilons:
        adj_tempEpsilon = convert_tempEpsilon(e, events)
        if verbose: print('{0} hours is {1} '
                                    'timesteps'.format(e, adj_tempEpsilon))
        tstep = events.index[-1]/(events.shape[0]-1)
        events_resampled = prep_data_for_convolution(events, adj_tempEpsilon)
        idx=np.arange(0,duration+adj_tempEpsilon*tstep,adj_tempEpsilon*tstep)
        ax.plot(idx, np.insert(list(events_resampled.iloc[:,0]), 0, 0),
                linewidth='1', label='tempEpsilon = {}'.format(e), alpha=0.75)
    ax.grid()
    ax.set_xlabel('Duration, [hours]')
    ax.set_ylabel('Incremental Excess, [inches/timestep]')
    ax.set_title('Excess Rainfall', size = 18)
    ax.legend(prop={'size': 9}) 


def plot_convEpsilon(events: pd.DataFrame, e1: str,
                e2: str, duration: int, tempEpsilon: float, 
                            convEpsilon: float, verbose: bool=True) -> float:
    '''Calculates the percent difference between the two curves at each 
       timestep, the maximum percent difference, and the summary statistic 
       (st1), and then plots the excess rainfall events and their percent 
       differences relative to convEpsilon.
    '''
    adj_tempEpsilon = convert_tempEpsilon(tempEpsilon, events)
    resampled = prep_data_for_convolution(events, adj_tempEpsilon)
    perc_dif = abs(resampled[e1]-resampled[e2])/((resampled[e1]
                                                    +resampled[e2])/2.0)*100.0
    max_perc_dif = perc_dif.max() 
    st1 = (convEpsilon-max_perc_dif)/convEpsilon
    if verbose: display(Markdown('<i>t<sub>c</sub></i>')); print(st1)
    tstep = events.index[-1]/(events.shape[0]-1)
    idx=np.arange(0,duration+adj_tempEpsilon*tstep,adj_tempEpsilon*tstep)
    fig, ax = plt.subplots(1, 2, figsize = (24, 5))
    ax[0].plot(idx, np.insert(list(resampled[e1]), 0, 0), label = e1)
    ax[0].plot(idx, np.insert(list(resampled[e2]), 0, 0), label = e2) 
    ax[0].set_title('Excess Rainfall Events', fontsize=18)
    ax[0].set_xlabel('Time, [hours]', fontsize = 18)
    ax[0].set_ylabel('Incremental Excess, [inches]', fontsize=18)
    ax[0].grid()
    ax[0].legend(prop={'size': 9})
    ax[1].plot(idx, np.insert(list(perc_dif), 0, 0), color = 'black',
                                                             label = '% Dif')
    ax[1].plot(idx[perc_dif.idxmax()+1], max_perc_dif, linestyle = '', 
                            marker = '.', markersize = 12, color = 'orange',
                label = 'Max % Dif = {}'.format(np.round(max_perc_dif, 2)))
    ax[1].plot(idx, np.ones(len(idx))*convEpsilon, color = 'red', 
                            label = 'convEpsilon = {0}'.format(convEpsilon))
    ax[1].set_title('Percent Difference between {0} and '
                                            '{1}'.format(e1, e2), fontsize=18)
    ax[1].set_xlabel('Time, [hours]', fontsize = 18)
    ax[1].set_ylabel('% Dif, [-]', fontsize=18)
    ax[1].grid()
    ax[1].legend(prop={'size': 9}) 
    return st1


def plot_volEpsilon(events: pd.DataFrame, e1: str, e2: str, duration: int, 
        tempEpsilon: float, volEpsilon: float, verbose: bool=True) -> float:
    '''Calculates the cumulative curves for the two events, the percent 
       difference at each timestep, the total percent difference (percent 
       difference at the final timestep), and the summary statistic (st2), 
       and then plots the cumulative excess rainfall events and their percent
       differences relative to volEpsilon.
    '''
    adj_tempEpsilon = convert_tempEpsilon(tempEpsilon, events)
    events_resampled = prep_data_for_convolution(events, adj_tempEpsilon)
    tstep = events.index[-1]/(events.shape[0]-1)
    idx = np.arange(0, duration+adj_tempEpsilon*tstep, adj_tempEpsilon*tstep)
    cum_e1 = events_resampled[e1].cumsum()
    cum_e2 = events_resampled[e2].cumsum()
    perc_dif = list(abs(cum_e1-cum_e2)/((cum_e1+cum_e2)/2.0)*100)
    perc_dif_total = perc_dif[-1]
    st2 = (volEpsilon-perc_dif_total)/volEpsilon
    if verbose: display(Markdown('<i>t<sub>v</sub></i>')); print(st2)
    fig, ax = plt.subplots(1, 2, figsize = (24, 5))
    ax[0].plot(idx, np.insert(list(cum_e1), 0, 0), label = e1)
    ax[0].plot(idx, np.insert(list(cum_e2), 0, 0), label = e2) 
    ax[0].set_title('Excess Rainfall Events', fontsize=18)
    ax[0].set_xlabel('Time, [hours]', fontsize = 18)
    ax[0].set_ylabel('Cumulative Excess, [inches]', fontsize=18)
    ax[0].grid()
    ax[0].legend(prop={'size': 9}) 
    ax[1].plot(idx, np.insert(perc_dif, 0, 0), color = 'black', 
                                                    label = 'Total % Dif')
    ax[1].plot(idx[-1], perc_dif_total, linestyle = '', marker = '.', 
                                            markersize = 12, color = 'orange', 
            label = 'Total % Dif = {}'.format(np.round(perc_dif_total, 2)))
    ax[1].plot(idx[-1], volEpsilon, linestyle = '', marker = '_', 
                                            markersize = 12, color = 'red', 
                                label = 'volEpsilon = {0}'.format(volEpsilon))
    ax[1].set_ylabel('% Dif, [-]', fontsize=18)
    ax[1].set_xlabel('Time, [hours]', fontsize = 18)
    ax[1].set_title('Percent Difference between {0} and '
                                            '{1}'.format(e1, e2), fontsize=18)
    ax[1].grid()
    ax[1].legend(prop={'size': 9}) 
    return st2


def plot_test_statistic(delta: int=0.05, vmin: float=-1.0, vmax: float=1.0) -> None:
    '''Plot a heat map of the test statistic as a funtion of the individual 
       summary statistics (t_c and t_v)
    '''
    tv, tc = np.mgrid[slice(0, 1 + delta, delta), 
                      slice(0, 1 + delta, delta)]
    t = 1 - np.sqrt((tc-1)**2+(tv-1)**2)
    fig, ax = plt.subplots(figsize = (9,6))
    c = ax.pcolormesh(tc, tv, t, cmap='RdBu', vmin=vmin, vmax=vmax)
    ax.set_xlabel('$t_c$', fontsize = 14)
    ax.set_ylabel('$t_v$', fontsize = 14)
    ax.set_title('Test Statistic as a Function of $t_c$ and '
                                                        '$t_v$', fontsize=18)
    fig.colorbar(c)


def plot_cum_precip_or_excess(df: pd.DataFrame, var: str='Precip') -> None:
    '''Plot the cumulative rainfall or excess rainfall verses time.
    '''
    assert var == 'Precip' or var == 'Excess'
    if var == 'Precip': 
        name = 'Precipitation'
    elif var == 'Excess':
        name = 'Excess Rainfall'
    fig, ax = plt.subplots(figsize = (24, 5))
    for col in df.columns:
        ax.plot(df[col]) 
    nevents = df.shape[1]
    ax.set_title('{0} {1} Events'.format(nevents, name), fontsize=18)
    ax.set_xlabel('Time (hours)', fontsize = 18)
    ax.set_ylabel('Cumulative {} (inches)'.format(var), fontsize=18)
    ax.grid()    


def plot_incr_excess(df: pd.DataFrame) -> None:
    '''Plot the incremental excess rainfall verses time.
    '''
    fig, ax = plt.subplots(figsize = (24, 5))
    for col in df.columns:
        ax.plot(df[col]) 
    nevents = df.shape[1]
    ax.set_title('{0} Excess Rainfall Events'.format(nevents), fontsize=18)
    ax.set_xlabel('Time (hours)', fontsize = 18)
    ax.set_ylabel('Incremental Excess (inches)', fontsize=18)
    ax.grid()


#---------------------------------------------------------------------------#