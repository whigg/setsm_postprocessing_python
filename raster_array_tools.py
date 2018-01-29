#!/usr/bin/env python2

# Version 3.0; Erik Husby; Polar Geospatial Center, University of Minnesota; 2017


from __future__ import division
import copy
import math
import operator
import os
import warnings
from collections import deque
from itertools import product
from PIL import Image
from subprocess import check_call
from traceback import print_exc
from warnings import warn

import cv2
import gdal, ogr, osr
import numpy as np
import scipy
import shapely.geometry
import shapely.ops
from osgeo import gdal_array
from scipy import ndimage as sp_ndimage
from scipy.spatial import ConvexHull
from skimage.draw import polygon_perimeter
from skimage import morphology as sk_morphology
from skimage.filters.rank import entropy
from skimage.util import unique_rows

# TODO: Remove `test` include once testing is complete.
import test

_outline = open("outline.c", "r").read()
_outline_every1 = open("outline_every1.c", "r").read()


RASTER_PARAMS = ['ds', 'shape', 'z', 'array', 'x', 'y', 'dx', 'dy', 'res', 'geo_trans', 'corner_coords', 'proj_ref', 'spat_ref', 'geom', 'geom_sr']


# warnings.simplefilter('always', UserWarning)
gdal.UseExceptions()

class RasterIOError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)

class UnsupportedDataTypeError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)

class InvalidArgumentError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)

class UnsupportedMethodError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)



#############
# Raster IO #
#############


# Legacy; Retained for quick instruction of useful GDAL raster information extraction methods.
def oneBandImageToArrayZXY_projRef(rasterFile):
    """
    Opens a single-band raster image as a NumPy 2D array [Z] and returns it along
    with [X, Y] coordinate ranges of pixels in the raster grid as NumPy 1D arrays
    and the projection definition string for the raster dataset in OpenGIS WKT format.
    """
    if not os.path.isfile(rasterFile):
        raise RasterIOError("No such rasterFile: '{}'".format(rasterFile))

    ds = gdal.Open(rasterFile, gdal.GA_ReadOnly)
    proj_ref = ds.GetProjectionRef()
    gt = ds.GetGeoTransform()

    xmin, ymax = gt[0], gt[3]
    dx, dy     = gt[1], gt[5]

    X = xmin + np.arange(ds.RasterXSize) * dx
    Y = ymax + np.arange(ds.RasterYSize) * dy

    Z = ds.GetRasterBand(1).ReadAsArray()

    return Z, X, Y, proj_ref


def openRaster(rasterFile_or_ds):
    ds = None
    if type(rasterFile_or_ds) == gdal.Dataset:
        ds = rasterFile_or_ds
    elif type(rasterFile_or_ds) == str:
        if not os.path.isfile(rasterFile_or_ds):
            raise RasterIOError("No such rasterFile: '{}'".format(rasterFile_or_ds))
        ds = gdal.Open(rasterFile_or_ds, gdal.GA_ReadOnly)
    else:
        raise InvalidArgumentError("Invalid input type for `rasterFile_or_ds`: {}".format(
                                   type(rasterFile_or_ds)))
    return ds


def getCornerCoords(gt, shape):
    top_left_x = np.full((5, 1), gt[0])
    top_left_y = np.full((5, 1), gt[3])
    top_left_mat = np.concatenate((top_left_x, top_left_y), axis=1)

    ysize, xsize = shape
    raster_XY_size_mat = np.array([
        [0, 0],
        [xsize, 0],
        [xsize, ysize],
        [0, ysize],
        [0, 0]
    ])

    gt_mat = np.array([
        [gt[1], gt[4]],
        [gt[2], gt[5]]
    ])

    return top_left_mat + np.dot(raster_XY_size_mat, gt_mat)


def coordsToWkt(corner_coords):
    return 'POLYGON (({}))'.format(
        ','.join([" ".join([str(c) for c in cc]) for cc in corner_coords])
    )


def wktToCoords(wkt):
    eval_str = 'np.array({})'.format(
        wkt.replace('POLYGON ','').replace('(','[').replace(')',']').replace(',','],[').replace(' ',',')
    )
    return eval(eval_str)


def extractRasterParams(rasterFile_or_ds, *params):
    ds = openRaster(rasterFile_or_ds)
    pset = set(params)
    invalid_pnames = pset.difference(set(RASTER_PARAMS))
    if invalid_pnames:
        raise InvalidArgumentError("Invalid parameter(s) for extraction: {}".format(invalid_pnames))

    if pset.intersection({'z', 'array'}):
        array_data = ds.GetRasterBand(1).ReadAsArray()
    if pset.intersection({'shape', 'x', 'y', 'corner_coords', 'geom', 'geom_sr'}):
        shape = (ds.RasterYSize, ds.RasterXSize) if 'array_data' not in vars() else array_data.shape
    if pset.intersection({'x', 'y', 'dx', 'dy', 'res', 'geo_trans', 'corner_coords', 'geom', 'geom_sr'}):
        geo_trans = ds.GetGeoTransform()
    if pset.intersection({'proj_ref', 'spat_ref', 'geom_sr'}):
        proj_ref = ds.GetProjectionRef()
    if pset.intersection({'corner_coords', 'geom', 'geom_sr'}):
        corner_coords = getCornerCoords(geo_trans, shape)
    if pset.intersection({'spat_ref', 'geom_sr'}):
        spat_ref = osr.SpatialReference(proj_ref) if proj_ref is not None else None
    if pset.intersection({'geom', 'geom_sr'}):
        geom = ogr.Geometry(wkt=coordsToWkt(corner_coords))

    value_list = []
    for pname in params:
        pname = pname.lower()
        value = None
        if pname == 'ds':
            value = ds
        elif pname == 'shape':
            value = shape
        elif pname in ('z', 'array'):
            value = array_data
        elif pname == 'x':
            value = geo_trans[0] + np.arange(shape[1]) * geo_trans[1]
        elif pname == 'y':
            value = geo_trans[3] + np.arange(shape[0]) * geo_trans[5]
        elif pname == 'dx':
            value = abs(geo_trans[1])
        elif pname == 'dy':
            value = abs(geo_trans[5])
        elif pname == 'res':
            value = abs(geo_trans[1]) if abs(geo_trans[1]) == abs(geo_trans[5]) else np.nan
        elif pname == 'geo_trans':
            value = geo_trans
        elif pname == 'corner_coords':
            value = corner_coords
        elif pname == 'proj_ref':
            value = proj_ref
        elif pname == 'spat_ref':
            value = spat_ref
        elif pname == 'geom':
            value = geom
        elif pname == 'geom_sr':
            value = geom.Clone() if 'geom' in params else geom
            if spat_ref is not None:
                value.AssignSpatialReference(spat_ref)
            else:
                warn("Spatial reference could not be extracted from raster dataset, "
                     "so extracted geometry has not been assigned a spatial reference.")
        value_list.append(value)

    if len(value_list) == 1:
        value_list = value_list[0]
    return value_list


# Legacy; Retained for a visual aid of equivalences between NumPy and GDAL data types.
# Use gdal_array.NumericTypeCodeToGDALTypeCode to convert from NumPy to GDAL data type.
def dtype_np2gdal(dtype_in, form_out='gdal', force_conversion=False):
    """
    Converts between input NumPy data type (dtype_in may be either
    NumPy 'dtype' object or already a string) and output GDAL data type.
    If form_out='numpy', the corresponding NumPy 'dtype' object will be
    returned instead, allowing for quick lookup by string name.
    If the third element of a dtype_dict conversion tuple is zero,
    that conversion of NumPy to GDAL data type is not recommended. However,
    the conversion may be forced with the argument force_conversion=True.
    """
    dtype_dict = {                                            # ---GDAL LIMITATIONS---
        'bool'      : (np.bool,       gdal.GDT_Byte,     0),  # GDAL no bool/logical/1-bit
        'int8'      : (np.int8,       gdal.GDT_Byte,     1),  # GDAL byte is unsigned
        'int16'     : (np.int16,      gdal.GDT_Int16,    1),
        'int32'     : (np.int32,      gdal.GDT_Int32,    1),
        'intc'      : (np.intc,       gdal.GDT_Int32,    1),  # np.intc ~= np.int32
        'int64'     : (np.int64,      gdal.GDT_Int32,    0),  # GDAL no int64
        'intp'      : (np.intp,       gdal.GDT_Int32,    0),  # intp ~= np.int64
        'uint8'     : (np.uint8,      gdal.GDT_Byte,     1),
        'uint16'    : (np.uint16,     gdal.GDT_UInt16,   1),
        'uint32'    : (np.uint32,     gdal.GDT_UInt32,   1),
        'uint64'    : (np.uint64,     gdal.GDT_UInt32,   0),  # GDAL no uint64
        'float16'   : (np.float16,    gdal.GDT_Float32,  1),  # GDAL no float16
        'float32'   : (np.float32,    gdal.GDT_Float32,  1),
        'float64'   : (np.float64,    gdal.GDT_Float64,  1),
        'complex64' : (np.complex64,  gdal.GDT_CFloat32, 1),
        'complex128': (np.complex128, gdal.GDT_CFloat64, 1),
    }
    errmsg_unsupported_dtype = "Conversion of NumPy data type '{}' to GDAL is not supported".format(dtype_in)

    try:
        dtype_tup = dtype_dict[str(dtype_in).lower()]
    except KeyError:
        raise UnsupportedDataTypeError("No such NumPy data type in lookup table: '{}'".format(dtype_in))

    if form_out.lower() == 'gdal':
        if dtype_tup[2] == 0:
            if force_conversion:
                print errmsg_unsupported_dtype
            else:
                raise UnsupportedDataTypeError(errmsg_unsupported_dtype)
        dtype_out = dtype_tup[1]
    elif form_out.lower() == 'numpy':
        dtype_out = dtype_tup[0]
    else:
        raise UnsupportedDataTypeError("The following output data type format is not supported: '{}'".format(form_out))

    return dtype_out


def saveArrayAsTiff(array, dest,
                    X=None, Y=None, proj_ref=None, geotrans_rot_tup=(0, 0),
                    like_rasterFile=None,
                    nodataVal=None, dtype_out=None):
    # FIXME: Rewrite docstring in new standard.
    """
    Saves a NumPy 2D array as a single-band raster image in GeoTiff format.
    Takes as input [X, Y] coordinate ranges of pixels in the raster grid as
    NumPy 1D arrays and geotrans_rot_tup specifying rotation coefficients
    in the output raster's geotransform tuple normally accessed via
    {GDALDataset}.GetGeoTransform()[[2, 4]] in respective index order.
    If like_rasterFile is provided, its geotransform and projection reference
    may be used for the output dataset and [X, Y, geotrans_rot_tup, proj_ref]
    should not be given.
    """
    dtype_gdal = None
    if dtype_out is not None:
        if type(dtype_out) == str:
            dtype_out = eval('np.{}'.format(dtype_out.lower()))
        dtype_gdal = gdal_array.NumericTypeCodeToGDALTypeCode(dtype_out)
        if dtype_gdal is None:
            raise InvalidArgumentError("Output array data type ({}) does not have equivalent "
                                       "GDAL data type and is not supported".format(dtype_out))

    dest_temp = dest.replace('.tif', '_temp.tif')

    dtype_in = array.dtype
    promote_dtype = None
    if dtype_in == np.bool:
        dtype_in = np.uint8  # np.bool values are 8-bit
    elif dtype_in == np.int8:
        promote_dtype = np.int16
    elif dtype_in == np.float16:
        promote_dtype = np.float32
    if promote_dtype is not None:
        warn("Input array data type ({}) does not have equivalent GDAL data type and is not "
             "supported, but will be safely promoted to {}".format(dtype_in, promote_dtype(1).dtype))
        array = array.astype(promote_dtype)
        dtype_in = promote_dtype

    if dtype_out is not None:
        if dtype_in != dtype_out:
            raise InvalidArgumentError("Input array data type ({}) differs from desired "
                                       "output data type ({})".format(dtype_in, dtype_out(1).dtype))
    else:
        dtype_gdal = gdal_array.NumericTypeCodeToGDALTypeCode(dtype_in)
        if dtype_gdal is None:
            raise InvalidArgumentError("Input array data type ({}) does not have equivalent "
                                       "GDAL data type and is not supported".format(dtype_in))

    if proj_ref is not None and type(proj_ref) == osr.SpatialReference:
        proj_ref = proj_ref.ExportToWkt()

    shape = array.shape
    geo_trans = None
    if like_rasterFile is not None:
        ds_like = gdal.Open(like_rasterFile, gdal.GA_ReadOnly)
        if shape[0] != ds_like.RasterYSize or shape[1] != ds_like.RasterXSize:
            raise InvalidArgumentError("Shape of `like_rasterFile` '{}' ({}, {}) does not match "
                                       "the shape of `array` ({})".format(
                like_rasterFile, ds_like.RasterYSize, ds_like.RasterXSize, shape)
            )
        geo_trans = ds_like.GetGeoTransform()
        if proj_ref is None:
            proj_ref = ds_like.GetProjectionRef()
    else:
        if shape[0] != Y.size or shape[1] != X.size:
            raise InvalidArgumentError("Lengths of [`Y`, `X`] grid coordinates ({}, {}) do not match "
                                       "the shape of `array` ({})".format(Y.size, X.size, shape))
        geo_trans = (X[0], X[1]-X[0], geotrans_rot_tup[0],
                     Y[0], geotrans_rot_tup[1], Y[1]-Y[0])

    # Create and write the output dataset to a temporary file.
    driver = gdal.GetDriverByName('GTiff')
    ds_out = driver.Create(dest_temp, shape[1], shape[0], 1, dtype_gdal)
    ds_out.SetGeoTransform(geo_trans)
    if proj_ref is not None:
        ds_out.SetProjection(proj_ref)
    else:
        warn("Missing projection reference for saved raster '{}'".format(dest))
    ds_out.GetRasterBand(1).WriteArray(array)
    ds_out = None  # Dereference dataset to initiate write to disk of intermediate image.

    ###################################################
    # Run gdal_translate with the following arguments #
    ###################################################
    args = [r'C:\OSGeo4W64\bin\gdal_translate', dest_temp, dest]

    if nodataVal is not None:
        args.extend(['-a_nodata', str(nodataVal)])  # Create internal nodata mask.

    args.extend(['-co', 'BIGTIFF=IF_SAFER'])        # Will create BigTIFF
                                                    # :: if the resulting file *might* exceed 4GB.
    args.extend(['-co', 'COMPRESS=LZW'])            # Do LZW compression on output image.
    args.extend(['-co', 'TILED=YES'])               # Force creation of tiled TIFF files.

    # print "Running: {}".format(' '.join(args))
    check_call(args)
    os.remove(dest_temp)  # Delete the intermediate image.



######################
# Array Calculations #
######################


def rotate_arrays_if_kernel_has_even_sidelength(array, kernel):
    for s in kernel.shape:
        if s % 2 == 0:
            return np.rot90(array, 2), np.rot90(kernel, 2), True
    return array, kernel, False


def fix_array_if_rotation_was_applied(array, rotation_flag):
    return np.rot90(array, 2) if rotation_flag else array


def array_round_proper(array, in_place):
    # Round half up for positive X.5,
    # round half down for negative X.5.

    if not in_place:
        array = np.copy(array)

    array_gt_zero = array > 0
    array_lt_zero = array < 0

    array[array_gt_zero] = np.floor(array + 0.5)[array_gt_zero]
    array[array_lt_zero] =  np.ceil(array - 0.5)[array_lt_zero]

    return array


def astype_round_and_crop(array, dtype_out, allow_modify_array=False):
    # This function is meant to replicate MATLAB array type casting.

    # The trivial case
    if dtype_out == np.bool:
        return array.astype(dtype_out)

    array_dtype_np = array.dtype.type
    dtype_out_np = dtype_out if type(dtype_out) != np.dtype else dtype_out.type

    if isinstance(array_dtype_np(1), np.floating) and isinstance(dtype_out_np(1), np.integer):
        # TODO: Consider replacing the following costly call with:
        # -t    np.around(array)
        array = array_round_proper(array, allow_modify_array)

    return astype_cropped(array, dtype_out_np, allow_modify_array)


def astype_cropped(array, dtype_out, allow_modify_array=False):
    # Check for overflow and underflow before converting data types,
    # cropping values to the range of `dtype_out`.

    # The trivial case
    if dtype_out == np.bool:
        return array.astype(dtype_out)

    dtype_out_np = dtype_out if type(dtype_out) != np.dtype else dtype_out.type
    dtype_info_fn = np.finfo if isinstance(dtype_out_np(1), np.floating) else np.iinfo
    dtype_out_min = dtype_info_fn(dtype_out_np).min
    dtype_out_max = dtype_info_fn(dtype_out_np).max

    array_clipped = array if allow_modify_array else None
    array_clipped = np.clip(array, dtype_out_min, dtype_out_max, array_clipped)

    return array_clipped.astype(dtype_out)


def interp2_fill_extrapolate(X, Y, Zi, Xi, Yi, fillval=np.nan):
    # Rows and columns of Zi outside the domain of Z are made NaN.
    # Assume X and Y coordinates are monotonically increasing/decreasing
    # so hopefully we only need to work a short way inwards from the edges.

    Xi_size = Xi.size
    Yi_size = Yi.size
    Xmin = np.min(X)
    Xmax = np.max(X)
    Ymin = np.min(Y)
    Ymax = np.max(Y)

    if X[0] == Xmin:
        # X-coords increase from left to right.
        x_lfttest_val = Xmin
        x_lfttest_op = operator.lt
        x_rgttest_val = Xmax
        x_rgttest_op = operator.gt
    else:
        # X-coords decrease from left to right.
        x_lfttest_val = Xmax
        x_lfttest_op = operator.gt
        x_rgttest_val = Xmin
        x_rgttest_op = operator.lt

    if Y[0] == Ymax:
        # Y-coords decrease from top to bottom.
        y_toptest_val = Ymax
        y_toptest_op = operator.gt
        y_bottest_val = Ymin
        y_bottest_op = operator.lt
    else:
        # Y-coords increase from top to bottom.
        y_toptest_val = Ymin
        y_toptest_op = operator.lt
        y_bottest_val = Ymax
        y_bottest_op = operator.gt

    i = 0
    while x_lfttest_op(Xi[i], x_lfttest_val) and i < Xi_size:
        Zi[:, i] = fillval
        i += 1
    i = -1
    while x_rgttest_op(Xi[i], x_rgttest_val) and i >= -Xi_size:
        Zi[:, i] = fillval
        i -= 1
    j = 0
    while y_toptest_op(Yi[j], y_toptest_val) and j < Yi_size:
        Zi[j, :] = fillval
        j += 1
    j = -1
    while y_bottest_op(Yi[j], y_bottest_val) and j >= -Yi_size:
        Zi[j, :] = fillval
        j -= 1

    return Zi


def interp2_gdal(X, Y, Z, Xi, Yi, interp, extrapolate=False):
    """
    Performs a resampling of the input NumPy 2D array [Z],
    from initial grid coordinates [X, Y] to final grid coordinates [Xi, Yi]
    (all four ranges as NumPy 1D arrays) using the desired interpolation method.
    To best match output with MATLAB's interp2 function, extrapolation of
    row and column data outside the [X, Y] domain of the input 2D array [Z]
    is manually wiped away and set to NaN by default when borderNaNs=True.
    """
    interp_dict = {
        'nearest'   : gdal.GRA_NearestNeighbour,
        'bilinear'  : gdal.GRA_Bilinear,
        'bicubic'   : gdal.GRA_Cubic,
        'spline'    : gdal.GRA_CubicSpline,
        'lanczos'   : gdal.GRA_Lanczos,
        'average'   : gdal.GRA_Average,
        'mode'      : gdal.GRA_Mode,
    }
    try:
        interp_gdal = interp_dict[interp]
    except KeyError:
        raise UnsupportedMethodError("`interp` must be one of {}, but was '{}'".format(interp_dict.vals(), interp))

    dtype_in = Z.dtype
    promote_dtype = None
    if dtype_in == np.bool:
        promote_dtype = np.uint8
    elif dtype_in == np.int8:
        promote_dtype = np.int16
    elif dtype_in == np.float16:
        promote_dtype = np.float32
    if promote_dtype is not None:
        warn("`array` data type ({}) does not have equivalent GDAL data type and is not "
             "supported, but will be safely promoted to {}".format(dtype_in, promote_dtype))
        Z = Z.astype(promote_dtype)
        dtype_in = promote_dtype

    dtype_gdal = gdal_array.NumericTypeCodeToGDALTypeCode(dtype_in)
    if dtype_gdal is None:
        raise InvalidArgumentError("`array` data type ({}) does not have equivalent "
                                   "GDAL data type and is not supported".format(dtype_in))

    mem_drv = gdal.GetDriverByName('MEM')

    ds_in = mem_drv.Create('', X.size, Y.size, 1, dtype_gdal)
    ds_in.SetGeoTransform((X[0], X[1]-X[0], 0,
                           Y[0], 0, Y[1]-Y[0]))
    ds_in.GetRasterBand(1).WriteArray(Z)

    ds_out = mem_drv.Create('', Xi.size, Yi.size, 1, dtype_gdal)
    ds_out.SetGeoTransform((Xi[0], Xi[1]-Xi[0], 0,
                            Yi[0], 0, Yi[1]-Yi[0]))

    gdal.ReprojectImage(ds_in, ds_out, '', '', interp_gdal)

    Zi = ds_out.GetRasterBand(1).ReadAsArray()

    if not extrapolate:
        interp2_fill_extrapolate(X, Y, Zi, Xi, Yi)

    return Zi


def interp2_scipy(X, Y, Z, Xi, Yi, interp, extrapolate=False,
                  griddata=False,
                  SBS=False,
                  RGI=False, extrap=True, RGI_fillVal=None,
                  CLT=False, CLT_fillVal=np.nan,
                  RBS=False):
    # TODO: Test this function.
    """
    Aims to provide similar functionality to interp2_gdal using SciPy's
    interpolation library. However, initial tests show that interp2_gdal
    both runs more quickly and produces output more similar to MATLAB's
    interp2 function for every method required by Ian's mosaicking script.
    griddata, SBS, and CLT interpolation methods are not meant to be used
    for the resampling of a large grid as is done here.
    """
    order = {
        'linear'   : 1,
        'quadratic': 2,
        'cubic'    : 3,
        'quartic'  : 4,
        'quintic'  : 5,
    }

    if griddata:
        # Supports nearest, linear, and cubic interpolation methods.
        # Has errored out with "QH7074 qhull warning: more than 16777215 ridges.
        #   ID field overflows and two ridges may have the same identifier."
        #   when used on large arrays. Fails to draw a convex hull of input points.
        # Needs more testing, but seems to handle NaN input. Output for linear and
        # cubic methods shows NaN borders when interpolating out of input domain.
        xx,  yy  = np.meshgrid(X, Y)
        xxi, yyi = np.meshgrid(Xi, Yi)
        Zi = scipy.interpolate.griddata((xx.flatten(),   yy.flatten()), Z.flatten(),
                                        (xxi.flatten(), yyi.flatten()), interp)
        Zi.resize((Yi.size, Xi.size))

    elif SBS:
        # Supports all 5 orders of spline interpolation.
        # Can't handle NaN input; results in all NaN output.
        xx,  yy  = np.meshgrid(X, Y)
        xxi, yyi = np.meshgrid(Xi, Yi)
        fn = scipy.interpolate.SmoothBivariateSpline(xx.flatten(), yy.flatten(), Z.flatten(),
                                                     kx=order[interp], ky=order[interp])
        Zi = fn.ev(xxi, yyi)
        Zi.resize((Yi.size, Xi.size))

    elif (interp == 'nearest') or ((interp == 'linear') and np.any(np.isnan(Z))) or RGI:
        # Supports nearest and linear interpolation methods.
        xxi, yyi = np.meshgrid(Xi, Yi[::-1])
        pi = np.column_stack((yyi.flatten(), xxi.flatten()))
        fn = scipy.interpolate.RegularGridInterpolator((Y[::-1], X), Z, method=interp,
                                                       bounds_error=(not extrap), fill_value=RGI_fillVal)
        Zi = fn(pi, method=interp)
        Zi.resize((Yi.size, Xi.size))

    elif ((interp == 'cubic') and np.any(np.isnan(Z))) or CLT:
        # Performs cubic interpolation of data,
        # but includes logic to first perform a nearest resampling of input NaNs.
        # Produces the same error as scipy.interpolate.griddata when used on large arrays.
        if np.any(np.isnan(Z)):
            Zi = interp2_scipy(X, Y, Z, Xi, Yi, 'nearest')
            Zi_data = np.where(~np.isnan(Zi))
            Z_data  = np.where(~np.isnan(Z))
            p  = np.column_stack((Z_data[0],   Z_data[1]))
            pi = np.column_stack((Zi_data[0], Zi_data[1]))
            fn = scipy.interpolate.CloughTocher2DInterpolator(p, Z[Z_data], fill_value=CLT_fillVal)
            Zi[Zi_data] = fn(pi)
        else:
            xx,  yy  = np.meshgrid(X, Y)
            xxi, yyi = np.meshgrid(Xi, Yi)
            p  = np.column_stack((xx.flatten(), yy.flatten()))
            pi = np.column_stack((xxi.flatten(), yyi.flatten()))
            fn = scipy.interpolate.CloughTocher2DInterpolator(p, Z.flatten(), fill_value=CLT_fillVal)
            Zi = fn(pi)
            Zi.resize((Yi.size, Xi.size))

    elif (interp in ('quadratic', 'quartic')) or RBS:
        # Supports all 5 orders of spline interpolation.
        # Can't handle NaN input; results in all NaN output.
        fn = scipy.interpolate.RectBivariateSpline(Y[::-1], X, Z,
                                                   kx=order[interp], ky=order[interp])
        Zi = fn(Yi[::-1], Xi, grid=True)

    else:
        # Supports linear, cubic, and quintic interpolation methods.
        # Can't handle NaN input; results in all NaN output.
        # Default interpolator for its presumed efficiency.
        fn = scipy.interpolate.interp2d(X, Y[::-1], Z, kind=interp)
        Zi = fn(Xi, Yi)

    if not extrapolate:
        interp2_fill_extrapolate(X, Y, Zi, Xi, Yi)

    return Zi


def imresize(array, size, interp='bicubic', float_resize=True, dtype_out='input',
             round_proper=True, one_dim_axis=1):
    """
    Resize an array.

    Parameters
    ----------
    array : ndarray, 2D
        The array to resize.
    size : shape tuple (2D) or scalar value
        If shape tuple, returns an array of this size.
        If scalar value, returns an array of shape
        that is `size` times the shape of `array`.
    interp : str; 'nearest', 'box', 'bilinear', 'hamming',
                  'bicubic', or 'lanczos'
        Interpolation method to use during resizing.
    float_resize : bool
        If True, convert the Pillow image of `array`
        to PIL mode 'F' before resizing.
        If False, allow the Pillow image to stay in its
        default PIL mode for resizing.
        The rounding scheme of resized integer images with
        integer PIL modes (e.g. 'L' or 'I') is unclear when
        compared with the same integer images in the 'F' PIL mode.
        This option has no effect when `array` dtype is floating.
    dtype_out : str; 'default' or 'input'
        If 'default' and `float_resize=True`, the returned
        array data type will be float32.
        If 'default' and `float_resize=False`, the returned
        array data type will be...
          - bool if `array` is bool
          - uint8 if `array` is uint8
          - int32 if `array` is integer other than uint8
          - float32 if `array` is floating
        If 'input', the returned array data type will be
        the same as `array` data type.
    round_proper : bool
        If the resized array is converted from floating
        to an integer data type (such as when `float_resize=True`
        and `dtype_out='input'`)...
          - If True, round X.5 values up to (X + 1).
          - If False, round X.5 values to nearest even integer to X.
    one_dim_axis : int, 0 or 1
        Which directional layout to give to a one-dimensional
        `array` before resizing.
        If 0, array runs vertically downwards across rows.
        If 1, array runs horizontally rightwards across columns.

    Returns
    -------
    imresize_old : ndarray, 2D, same type as `array`
        The resized array.

    See Also
    --------
    imresize_old

    Notes
    -----
    This function is a wrapper for Pillow's `PIL.Image.resize` function [1]
    meant to replicate MATLAB's `imresize` function [2].

    References
    ----------
    .. [1] http://pillow.readthedocs.io/en/3.1.x/reference/Image.html
    .. [4] https://www.mathworks.com/help/images/ref/imresize.html

    """
    array_backup = array
    array_dtype_in = array.dtype

    interp_dict = {
        'nearest'  : Image.NEAREST,
        'box'      : Image.BOX,
        'bilinear' : Image.BILINEAR,
        'hamming'  : Image.HAMMING,
        'bicubic'  : Image.BICUBIC,
        'lanczos'  : Image.LANCZOS,
    }
    try:
        interp_pil = interp_dict[interp]
    except KeyError:
        raise UnsupportedMethodError("`interp` must be one of {}, but was '{}'".format(interp_dict.vals(), interp))

    dtype_out_choices = ('default', 'input')
    if dtype_out not in dtype_out_choices:
        raise InvalidArgumentError("`dtype_out` must be one of {}, but was '{}'".format(dtype_out_choices, dtype_out))

    # Handle 1D array input.
    one_dim_flag = False
    if array.ndim == 1:
        one_dim_flag = True
        if one_dim_axis == 0:
            array_shape_1d = (array.size, 1)
        elif one_dim_axis == 1:
            array_shape_1d = (1, array.size)
        else:
            raise InvalidArgumentError("`one_dim_axis` must be either 0 or 1")
        array = np.reshape(array, array_shape_1d)

    # If a resize factor is provided for size, round up the x, y pixel
    # sizes for the output array to match MATLAB's imresize function.
    new_shape = size if type(size) == tuple else tuple(np.ceil(np.dot(size, array.shape)).astype(int))
    if one_dim_flag and type(size) != tuple:
        new_shape = (new_shape[0], 1) if one_dim_axis == 0 else (1, new_shape[1])
    # The trivial case
    if new_shape == array.shape:
        return array_backup

    # Convert NumPy array to Pillow Image.
    image = None
    if array_dtype_in == np.bool:
        if float_resize:
            image = Image.fromarray(array, 'L')
        else:
            image = Image.frombytes(mode='1', size=array.shape[::-1], data=np.packbits(array, axis=1))
    else:
        if array_dtype_in == np.float16:
            array = array.astype(np.float32)
        if not float_resize:
            if array_dtype_in == np.uint16:
                array = array.astype(np.int32)
            elif array_dtype_in == np.uint32:
                if np.any(array > np.iinfo(np.int32).max):
                    raise InvalidArgumentError("`array` of uint32 cannot be converted to int32")
        image = Image.fromarray(array)

    if float_resize and image.mode != 'F':
        image = image.convert('F')

    # Resize array.
    image = image.resize(tuple(list(new_shape)[::-1]), interp_pil)

    # Set "default" data type for reading data into NumPy array.
    if image.mode == '1':
        dtype_out_np = np.bool
        image = image.convert("L")
    elif image.mode == 'L':
        dtype_out_np = np.uint8
    elif image.mode == 'I':
        dtype_out_np = np.int32
    elif image.mode == 'F':
        dtype_out_np = np.float32

    # Convert Pillow Image to NumPy array.
    result = np.fromstring(image.tobytes(), dtype=dtype_out_np)
    result = result.reshape((image.size[1], image.size[0]))

    # Clean up result array.
    if dtype_out == 'input' and result.dtype != array_dtype_in:
        if round_proper:
            result = astype_round_and_crop(result, array_dtype_in, allow_modify_array=True)
        else:
            result = astype_cropped(result, array_dtype_in, allow_modify_array=True)
    if one_dim_flag:
        result_size_1d = new_shape[0] if one_dim_axis == 0 else new_shape[1]
        result = np.reshape(result, result_size_1d)

    return result


def imresize_old(array, size, interp='bicubic', method='pil', dtype_out='input',
                 one_dim_axis=1):
    """
    Resize an array.

    Parameters
    ----------
    array : ndarray, 2D
        The array to resize.
    size : shape tuple (2D) or scalar value
        If shape tuple, returns an array of this size.
        If scalar value, returns an array of shape
        that is `size` times the shape of `array`.
    interp : str
        Interpolation method to use during resizing.
        See documentation for a particular `method`.
    method : str; 'auto', 'scipy', 'gdal', 'cv2'
        Specifies which method used to perform resizing.
        'auto' ----- ??????????????
        'cv2' ------ cv2.resize [1]
        'gdal' ----- interp2_gdal (local, utilizes gdal.ReprojectImage [2])
        'pil' ------ PIL.Image.resize [3]
        'scipy' ---- scipy.misc.imresize (WILL BE RETIRED SOON) [4]
    dtype_out : str; 'float' or 'input'
        If 'float' and `array` data type is floating,
        data type of the returned array is the same.
        If 'float' and `array` data type is not floating,
        data type of the returned array is float32.
        If 'input', data type of the returned array is
        the same as `array`.
    one_dim_axis : int, 0 or 1
        Which directional layout to give to a one-dimensional
        `array` before resizing.
        If 0, array runs vertically downwards across rows.
        If 1, array runs horizontally rightwards across columns.

    Returns
    -------
    imresize_old : ndarray, 2D, same type as `array`
        The resized array.

    See Also
    --------
    imresize

    Notes
    -----
    This function is meant to replicate MATLAB's `imresize` function [5].

    References
    ----------
    .. [1] https://docs.opencv.org/2.4/modules/imgproc/doc/geometric_transformations.html#void resize(InputArray src, OutputArray dst, Size dsize, double fx, double fy, int interpolation)
    .. [2] http://gdal.org/java/org/gdal/gdal/gdal.html#ReprojectImage-org.gdal.gdal.Dataset-org.gdal.gdal.Dataset-java.lang.String-java.lang.String-int-double-double-org.gdal.gdal.ProgressCallback-java.util.Vector-
           https://svn.osgeo.org/gdal/trunk/autotest/alg/reproject.py
    .. [3] http://pillow.readthedocs.io/en/3.1.x/reference/Image.html
    .. [4] https://docs.scipy.org/doc/scipy/reference/generated/scipy.misc.imresize.html
    .. [5] https://www.mathworks.com/help/images/ref/imresize.html

    """
    array_backup = array

    method_choices = ('cv2', 'gdal', 'pil', 'scipy')
    dtype_out_choices = ('float', 'input')

    if method not in method_choices:
        raise UnsupportedMethodError("`method` must be one of {}, "
                                     "but was '{}'".format(method_choices, method))
    if dtype_out not in dtype_out_choices:
        raise InvalidArgumentError("`dtype_out` must be one of {}, "
                                   "but was '{}'".format(dtype_out_choices, dtype_out))

    # Handle 1D array input.
    one_dim_flag = False
    if array.ndim == 1:
        one_dim_flag = True
        if one_dim_axis == 0:
            array_shape_1d = (array.size, 1)
        elif one_dim_axis == 1:
            array_shape_1d = (1, array.size)
        else:
            raise InvalidArgumentError("`one_dim_axis` must be either 0 or 1")
        array = np.reshape(array, array_shape_1d)

    # If a resize factor is provided for size, round up the x, y pixel
    # sizes for the output array to match MATLAB's imresize function.
    new_shape = size if type(size) == tuple else tuple(np.ceil(np.dot(size, array.shape)).astype(int))
    if one_dim_flag and type(size) != tuple:
        new_shape = (new_shape[0], 1) if one_dim_axis == 0 else (1, new_shape[1])
    # The trivial case
    if new_shape == array.shape:
        return array_backup

    array_dtype_in = array.dtype
    dtype_out_np = None
    if dtype_out == 'float':
        dtype_out_np = array_dtype_in if isinstance(array_dtype_in.type(1), np.floating) else np.float32
    elif dtype_out == 'input':
        dtype_out_np = array_dtype_in

    if method == 'cv2':
        interp_dict = {
            'nearest'  : cv2.INTER_NEAREST,
            'area'     : cv2.INTER_AREA,
            'bilinear' : cv2.INTER_LINEAR,
            'bicubic'  : cv2.INTER_CUBIC,
            'lanczos'  : cv2.INTER_LANCZOS4,
        }
        try:
            interp_cv2 = interp_dict[interp]
        except KeyError:
            raise InvalidArgumentError("For `method=cv2`, `interp` must be one of {}, "
                                       "but was '{}'".format(interp_dict.vals(), interp))
        result = cv2.resize(array, tuple(list(new_shape)[::-1]), interpolation=interp_cv2)

    elif method == 'gdal':
        # Set up grid coordinate arrays, then run interp2_gdal.
        X = np.arange(array.shape[1]) + 1
        Y = np.arange(array.shape[0]) + 1
        Xi = np.linspace(X[0], X[-1] + (X[1]-X[0]), num=(new_shape[1] + 1))[0:-1]
        Yi = np.linspace(Y[0], Y[-1] + (Y[1]-Y[0]), num=(new_shape[0] + 1))[0:-1]
        result = interp2_gdal(X, Y, array, Xi, Yi, interp, extrapolate=False)

    elif method == 'pil':
        return imresize(array, new_shape, interp)

    elif method == 'scipy':
        PILmode = 'L' if array.dtype in (np.bool, np.uint8) else 'F'
        if PILmode == 'L' and array.dtype != np.uint8:
            array = array.astype(np.uint8)
        result = scipy.misc.imresize(array, new_shape, interp, PILmode)

    # Clean up result array.
    if result.dtype != dtype_out_np:
        result = astype_round_and_crop(result, dtype_out_np, allow_modify_array=True)
    if one_dim_flag:
        result_size_1d = new_shape[0] if one_dim_axis == 0 else new_shape[1]
        result = np.reshape(result, result_size_1d)

    return result


def conv2_slow(array, kernel, shape='full', default_double_out=True, zero_border=True,
               fix_float_zeros=True, nan_over_zero=True, allow_flipped_processing=True):
    """
    Convolve two 2D arrays.

    Parameters
    ----------
    array : ndarray, 2D
        Primary array to convolve.
    kernel : ndarray, 2D, smaller shape than `array`
        Secondary, smaller array to convolve with `array`.
    shape : str; 'full', 'same', or 'valid'
        See documentation for `scipy.signal.convolve` [1].
    default_double_out : bool
        If True and `array` is not of floating data type,
        casts the result to float64 before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [2].
    zero_border : bool
        When `kernel` hangs off the edges of `array`
        during convolution calculations...
        If True, pixels beyond the edges of `array`
        are extrapolated as zeros.
        If False, pixels beyond the edges of `array`
        are extrapolated as the value of the closest edge pixel.
        This option only applies when `shape='same'`,
        since a zero border is required when `shape='full'`
        and does not make sense when `shape='valid'`.
    fix_float_zeros : bool
        To correct for FLOP error in convolution where the result
        should be zero but isn't, immediately following convolution
        map array values between -1.0e-12 and +1.0e-11 to zero.
    nan_over_zero : bool
        If True, let NaN x 0 = NaN in convolution computation.
        If False, let NaN x 0 = 0 in convolution computation.
    allow_flipped_processing : bool
        If True and at least one of `kernel`'s side lengths is even,
        rotate both `array` `kernel` 180 degrees before performing convolution,
        then rotate the result array 180 degrees before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [2].

    Returns
    -------
    conv2_slow : ndarray, 2D
        A 2D array containing the convolution of the input array and kernel.

    See Also
    --------
    conv2

    Notes
    -----
    This function is meant to replicate MATLAB's conv2 function [2].

    Scipy's convolution function cannot handle NaN input as it results in all NaN output.
    In comparison, MATLAB's conv2 function takes a sensible approach by letting NaN win out
    in all calculations involving pixels with NaN values in the input array.
    To replicate this, we set all NaN values to zero before performing convolution,
    then mask our result array with NaNs according to a binary dilation of ALL NaN locations
    in the input array, dilating using a structure of ones with same shape as the provided kernel.

    For large arrays, this function will use an FFT method for convolution that results in
    FLOP errors on the order of 10^-12. For this reason, a floating result array will have
    all resulting pixel values between -1.0e-12 and 10.0e-12 set to zero.

    References
    ----------
    .. [1] https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.convolve.html
    .. [2] https://www.mathworks.com/help/matlab/ref/conv2.html

    """
    shape_choices = ('full', 'same', 'valid')
    if shape not in shape_choices:
        raise InvalidArgumentError("`shape` must be one of {}, but was '{}'".format(shape_choices, shape))

    if default_double_out:
        dtype_out = None
        if isinstance(array.dtype.type(1), np.floating):
            dtype_out = array.dtype
            if (isinstance(kernel.dtype.type(1), np.floating)
                and int(str(kernel.dtype).replace('float', '')) > int(str(dtype_out).replace('float', ''))):
                warn("Since default_double_out=True, kernel with floating dtype ({}) at greater precision than "
                     "array floating dtype ({}) is cast to array dtype".format(kernel.dtype, dtype_out))
                kernel = kernel.astype(dtype_out)
        else:
            dtype_out = np.float64

    if kernel.dtype == np.bool:
        warn("Boolean data type for kernel is not supported, casting to float32")
        kernel = kernel.astype(np.float32)

    rotation_flag = False
    if allow_flipped_processing:
        array, kernel, rotation_flag = rotate_arrays_if_kernel_has_even_sidelength(array, kernel)

    # Take a record of where all NaN values are located
    # before setting the values of those pixels to zero.
    fixnans_flag = False
    if isinstance(array.dtype.type(1), np.floating):
        array_nans = np.isnan(array)
        if np.any(array_nans):
            fixnans_flag = True
            array[array_nans] = 0
        else:
            del array_nans

    # Edge settings
    array_backup = array
    if (fixnans_flag and shape != 'same') or (shape == 'same' and not zero_border):
        if shape in ('full', 'same'):
            pady_top, padx_lft = (np.array(kernel.shape) - 1) / 2
            pady_bot, padx_rht = np.array(kernel.shape) / 2
        elif shape == 'valid':
            pady_top, padx_lft = np.array(kernel.shape) / 2
            pady_bot, padx_rht = (np.array(kernel.shape) - 1) / 2
        pady_top, padx_lft = int(pady_top), int(padx_lft)
        pady_bot, padx_rht = int(pady_bot), int(padx_rht)
        if shape == 'same':  # and not zero_border
            array = np.pad(array, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'edge')

    # Perform convolution.
    method = scipy.signal.choose_conv_method(array, kernel, shape)
    result = scipy.signal.convolve(array, kernel, shape, method)
    if method != 'direct' and fix_float_zeros and isinstance(result.dtype.type(1), np.floating):
        # Fix FLOP error from FFT method where we assume zero was the desired result.
        result[(-1.0e-12 < result) & (result < 10.0e-12)] = 0

    # Apply dilation of original NaN pixels to result.
    if fixnans_flag:
        array_nans_backup = array_nans
        if shape != 'same' or not zero_border:
            if shape == 'full':
                array_nans = np.pad(array_nans, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'constant', constant_values=0)
            elif shape == 'same':  # and not zero_border
                array_nans = np.pad(array_nans, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'edge')

        dilate_structure = np.ones(kernel.shape, dtype=np.uint8)
        if not nan_over_zero:
            dilate_structure[kernel == 0] = 0

        array_nans_dilate = imdilate(array_nans, dilate_structure)
        if shape == 'valid':
            pady_bot = -pady_bot if pady_bot > 0 else None
            padx_rht = -padx_rht if padx_rht > 0 else None
            array_nans_dilate = array_nans_dilate[pady_top:pady_bot, padx_lft:padx_rht]

        result[array_nans_dilate] = np.nan

        # Return the input array to its original state.
        array_backup[array_nans_backup] = np.nan

    # Clean up the result array.
    if shape == 'same' and not zero_border:
        pady_bot = -pady_bot if pady_bot > 0 else None
        padx_rht = -padx_rht if padx_rht > 0 else None
        result = result[pady_top:pady_bot, padx_lft:padx_rht]
    # FIXME: Make returned data type function like conv2.
    if default_double_out and result.dtype != dtype_out:
        result = result.astype(dtype_out)

    return fix_array_if_rotation_was_applied(result, rotation_flag)


def conv2(array, kernel, shape='full', conv_depth='default', zero_border=True,
          fix_float_zeros=True, nan_over_zero=True, allow_flipped_processing=True):
    """
    Convolve two 2D arrays.

    Parameters
    ----------
    array : ndarray, 2D
        Primary array to convolve.
    kernel : ndarray, 2D, smaller shape than `array`
        Secondary, smaller array to convolve with `array`.
    shape : str; 'full', 'same', or 'valid'
        See documentation for MATLAB's `conv2` function [2].
    conv_depth : str; 'default', 'input', 'int16', 'single'/'float32', or 'double'/'float64'
        Sets the data type depth of the convolution function filter2D,
        and correspondingly sets the data type of the returned array.
        'default': If `array` is of floating data type,
          returns an array of that data type, otherwise returns
          an array of float64.
        'input': Returns an array of the same data type as `array`.
        'int16': Returns an array of int16.
        'single'/'float32': Returns an array of float32.
        'double'/'float64': Returns an array of float64.
        BEWARE: 'float32' option results in
    zero_border : bool
        When `kernel` hangs off the edges of `array`
        during convolution calculations...
        If True, pixels beyond the edges of `array`
        are extrapolated as zeros.
        If False, pixels beyond the edges of `array`
        are extrapolated as the value of the closest edge pixel.
        This option only applies when `shape='same'`,
        since a zero border is required when `shape='full'`
        and does not make sense when `shape='valid'`.
    fix_float_zeros : bool
        To correct for FLOP error in convolution where the result
        should be zero but isn't, immediately following convolution
        map array values between...
        - float32 (single):
            -1.0e-6 and +1.0e-6 to zero.
        - float54 (double):
            -1.0e-15 and +1.0e-15 to zero.
    nan_over_zero : bool
        If True, let NaN x 0 = NaN in convolution computation.
        If False, let NaN x 0 = 0 in convolution computation.
    allow_flipped_processing : bool
        If True and at least one of `kernel`'s side lengths is even,
        rotate both `array` `kernel` 180 degrees before performing convolution,
        then rotate the result array 180 degrees before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [2].

    Returns
    -------
    conv2 : ndarray, 2D
        Array containing the convolution of input array and kernel.

    See Also
    --------
    conv2_slow

    Notes
    -----
    This function utilizes a fast OpenCV function `filter2D` [1]
    as a means to replicate MATLAB's `conv2` function [2].

    References
    ----------
    .. [1] https://docs.opencv.org/2.4/modules/imgproc/doc/filtering.html#filter2d
    .. [2] https://www.mathworks.com/help/matlab/ref/conv2.html

    """
    shape_choices = ('full', 'same', 'valid')
    if shape not in shape_choices:
        raise InvalidArgumentError("`shape` must be one of {}, but was '{}'".format(shape_choices, shape))

    conv_depth_choices = ('default', 'input', 'int16', 'single', 'float32', 'double', 'float64')
    if conv_depth not in conv_depth_choices:
        raise InvalidArgumentError("`conv_depth` must be one of {}, but was '{}'".format(conv_depth_choices, conv_depth))

    cv2_array_dtypes = [np.uint8, np.int16, np.uint16, np.float32, np.float64]
    cv2_kernel_dtypes = [np.int8, np.uint8, np.int16, np.uint16, np.int32, np.int64, np.uint64, np.float32, np.float64]

    # Check array data type.
    array_error = False
    array_dtype_in = array.dtype
    if array_dtype_in not in cv2_array_dtypes:
        array_dtype_errmsg = ("Fast convolution method only allows array dtypes {}, "
                              "but was {}".format([str(d(1).dtype) for d in cv2_array_dtypes], array.dtype))
        # Only cast to a higher data type for safety.
        array_dtype_cast = None
        if array_dtype_in == np.bool:
            array_dtype_cast = np.uint8
        elif array_dtype_in == np.int8:
            array_dtype_cast = np.int16
        elif array_dtype_in == np.float16:
            array_dtype_cast = np.float32
        if array_dtype_cast is None:
            array_error = True

    # Check kernel data type.
    kernel_error = False
    kernel_dtype_in = kernel.dtype
    if kernel_dtype_in not in cv2_kernel_dtypes:
        kernel_dtype_errmsg = ("Fast convolution method only allows kernel dtypes {} "
                               "but was {}".format([str(d(1).dtype) for d in cv2_kernel_dtypes], kernel.dtype))
        # Only cast to a higher data type for safety.
        kernel_dtype_cast = None
        if kernel_dtype_in == np.bool:
            kernel_dtype_cast = np.uint8
        elif kernel_dtype_in == np.uint32:
            kernel_dtype_cast = np.uint64
        elif kernel_dtype_in == np.float16:
            kernel_dtype_cast = np.float32
        if kernel_dtype_cast is None:
            kernel_error = True

    # Fall back to old (slower) conv2 function
    # if array or kernel data type is unsupported.
    if array_error or kernel_error:
        dtype_errmsg = "{}{}{}".format(array_dtype_errmsg * array_error,
                                       "\n" * (array_error * kernel_error),
                                       kernel_dtype_errmsg * kernel_error)
        if conv_depth != 'default':
            raise UnsupportedDataTypeError(dtype_errmsg + "\nSince conv_depth ('{}') != 'default', "
                                           "cannot fall back to other method".format(conv_depth))
        warn(dtype_errmsg + "\n-> Falling back to slower, less exact method")
        return conv2_slow(array, kernel, shape, True,
                          nan_over_zero, allow_flipped_processing)

    # Promote array or kernel to higher data type if necessary
    # to continue with faster and more reliable convolution method.
    array_casted = False
    if 'array_dtype_cast' in vars():
        if array_dtype_in != np.bool:
            warn(array_dtype_errmsg + "\n-> Casting array from {} to {} for processing".format(
                 array_dtype_in, array_dtype_cast(1).dtype))
        array = array.astype(array_dtype_cast)
        array_casted = True
    if 'kernel_dtype_cast' in vars():
        if array_dtype_in != np.bool:
            warn(kernel_dtype_errmsg + "\n-> Casting kernel from {} to {} for processing".format(
                 kernel_dtype_in, kernel_dtype_cast(1).dtype))
        kernel = kernel.astype(kernel_dtype_cast)

    # Set convolution depth and output data type.
    ddepth = None
    dtype_out = None
    conv_dtype_error = False
    if conv_depth == 'default':
        if isinstance(array_dtype_in.type(1), np.floating):
            ddepth = -1
            dtype_out = array_dtype_in
        else:
            ddepth = cv2.CV_64F
            dtype_out = np.float64
    elif conv_depth == 'input':
        ddepth = -1
        dtype_out = array_dtype_in
    elif conv_depth == 'int16':
        ddepth = cv2.CV_16S
        dtype_out = np.int16
        if array.dtype != np.uint8:
            conv_dtype_error = True
            conv_dtype_errmsg = "conv_depth can only be 'int16' if array dtype is uint8"
    elif conv_depth in ('single', 'float32'):
        ddepth = cv2.CV_32F
        dtype_out = np.float32
        if array.dtype == np.float64:
            conv_dtype_error = True
            conv_dtype_errmsg = "conv_depth can only be 'single'/'float32' if array dtype is not float64"
    elif conv_depth in ('double', 'float64'):
        ddepth = cv2.CV_64F
        dtype_out = np.float64
        if array.dtype == np.float32:
            conv_dtype_errmsg = "conv_depth can only be 'double'/'float64' if array dtype is not float32"
            warn(conv_dtype_errmsg + "\n-> Casting array from float32 to float64 for processing")
            array = array.astype(np.float64)
            array_casted = True

    if conv_dtype_error:
        raise UnsupportedDataTypeError(conv_dtype_errmsg)

    rotation_flag = False
    if allow_flipped_processing:
        array, kernel, rotation_flag = rotate_arrays_if_kernel_has_even_sidelength(array, kernel)

    # Take a record of where all NaN values are located
    # before setting the values of those pixels to zero.
    fixnans_flag = False
    if isinstance(array.dtype.type(1), np.floating):
        array_nans = np.isnan(array)
        if np.any(array_nans):
            fixnans_flag = True
            if not array_casted:
                array_backup = array
            array[array_nans] = 0
        else:
            del array_nans

    # Edge settings
    if shape != 'same':
        if shape == 'full':
            pady_top, padx_lft = (np.array(kernel.shape) - 1) / 2
            pady_bot, padx_rht = np.array(kernel.shape) / 2
        elif shape == 'valid':
            pady_top, padx_lft = np.array(kernel.shape) / 2
            pady_bot, padx_rht = (np.array(kernel.shape) - 1) / 2
        pady_top, padx_lft = int(pady_top), int(padx_lft)
        pady_bot, padx_rht = int(pady_bot), int(padx_rht)
        if shape == 'full':
            array = np.pad(array, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'constant', constant_values=0)

    # Perform convolution.
    result = cv2.filter2D(array, ddepth, np.rot90(kernel, 2),
                          borderType=(cv2.BORDER_CONSTANT if zero_border else cv2.BORDER_REPLICATE))
    if fix_float_zeros and isinstance(result.dtype.type(1), np.floating):
        # Fix FLOP error where we assume zero was the desired result.
        if result.dtype == np.float32:
            result[(-1.0e-6 < result) & (result < 1.0e-6)] = 0
        elif result.dtype == np.float64:
            result[(-1.0e-15 < result) & (result < 1.0e-15)] = 0
    if result.dtype != dtype_out:
        result = astype_round_and_crop(result, dtype_out, allow_modify_array=True)

    # Crop result if necessary.
    if shape == 'valid':
        if pady_bot >= 0:
            pady_bot = -pady_bot if pady_bot > 0 else None
        if padx_rht >= 0:
            padx_rht = -padx_rht if padx_rht > 0 else None
        result = result[pady_top:pady_bot, padx_lft:padx_rht]

    # Apply dilation of original NaN pixels to result.
    if fixnans_flag:
        array_nans_backup = array_nans
        if shape != 'same' or not zero_border:
            if shape == 'full':
                array_nans = np.pad(array_nans, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'constant', constant_values=0)
            elif shape == 'same':  # and not zero_border
                array_nans = np.pad(array_nans, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'edge')

        dilate_structure = np.ones(kernel.shape, dtype=np.uint8)
        if not nan_over_zero:
            dilate_structure[kernel == 0] = 0

        array_nans_dilate = imdilate(array_nans, dilate_structure)
        if shape == 'valid':
            if pady_bot >= 0:
                pady_bot = -pady_bot if pady_bot > 0 else None
            if padx_rht >= 0:
                padx_rht = -padx_rht if padx_rht > 0 else None
            array_nans_dilate = array_nans_dilate[pady_top:pady_bot, padx_lft:padx_rht]

        result[array_nans_dilate] = np.nan

        # Return the input array to its original state.
        if not array_casted:
            array_backup[array_nans_backup] = np.nan

    return fix_array_if_rotation_was_applied(result, rotation_flag)


def filter2(array, kernel, shape='full', zero_border=True, conv_depth='default',
            nan_over_zero=True, allow_flipped_processing=True):
    """
    Apply the (convolution) filter kernel to an array in 2D.

    See documentation for `conv2`, but replace the word "convolve" with "filter".

    Notes
    -----
    The mathematical convolution function (as implemented in conv2)
    rotates the kernel 180 degrees before sliding it over the array
    and performing the multiplications/additions.

    """
    return conv2(array, np.rot90(kernel, 2), shape, zero_border, conv_depth,
                 nan_over_zero, allow_flipped_processing)


def moving_average(array, nhood, shape='same', conv_depth='default',
                   allow_flipped_processing=True):
    """
    Calculate the moving average over an array.

    Parameters
    ----------
    array : ndarray, 2D
        Array for which to calculate the moving average.
    nhood : positive int, tuple like `array.shape`, or (ndarray, 2D)
        If an integer / tuple, specifies the side length / shape
        of structure (of ones) to be used as structure for moving window.
        If ndarray, must be a binary array with True/1-valued elements
        specifying the structure for moving window.
    shape :
        See documentation for `conv2`.
    conv_depth : str; 'default', 'single', or 'double'
        Specifies the floating data type of the convolution kernel.
        See documentation for `conv2`.
    allow_flipped_processing : bool
        See documentation for `conv2` function.

    See Also
    --------
    conv2
    conv2_slow

    Returns
    -------
    moving_average : ndarray, 2D
        Array containing the moving average of the input array.

    """
    conv_dtype_choices = ('default', 'single', 'double')

    structure = None
    if type(nhood) in (int, tuple):
        size = nhood
    elif type(nhood) == np.ndarray:
        structure = nhood
    else:
        raise InvalidArgumentError("`nhood` type may only be int, tuple, or ndarray, "
                                   "but was {} (nhood={})".format(type(nhood), nhood))

    if conv_depth not in conv_dtype_choices:
        raise UnsupportedDataTypeError("float_dtype must be one of {}, "
                                       "but was {}".format(conv_dtype_choices, conv_depth))

    if conv_depth == 'default':
        float_dtype = np.float32 if array.dtype == np.float32 else np.float64
    else:
        float_dtype = np.float32 if conv_depth == 'single' else np.float64

    if structure is not None:
        if not np.any(structure):
            # The trivial case,
            # must be handled to prevent divide by zero error.
            return np.zeros_like(array, float_dtype)
        if np.any(~np.logical_or(structure == 0, structure == 1)):
            raise InvalidArgumentError("`structure` may only contain zeros and ones")
    else:
        if type(size) == int:
            structure = np.ones((size, size), dtype=float_dtype)
        elif type(size) == tuple:
            structure = np.ones(size, dtype=float_dtype)

    conv_kernel = np.rot90(np.divide(structure, np.sum(structure), dtype=float_dtype), 2)

    return conv2(array, conv_kernel, shape, conv_depth=conv_depth,
                 allow_flipped_processing=allow_flipped_processing)


def conv_binary_structure_prevent_overflow(array, structure):
    # Get upper bound on minimum positive bitdepth for convolution.
    conv_bitdepth_pos = math.log(np.prod(structure.shape)+1, 2)
    dtype_bitdepths_pos = (1, 7, 8, 15, 16, 31, 32, 63, 64)
    for b in dtype_bitdepths_pos:
        if conv_bitdepth_pos <= b:
            conv_bitdepth_pos = b
            break

    # Parse input array and structure data type for bitdepth.
    input_bitdepth_pos = 0
    for arr in (array, structure):
        arr_gentype = arr.dtype.type(1)
        if arr.dtype == np.bool:
            arr_posbits = 1
        elif isinstance(arr_gentype, np.int):
            arr_posbits = int(str(arr.dtype).replace('int', '')) - 1
        elif isinstance(arr_gentype, np.uint):
            arr_posbits = int(str(arr.dtype).replace('uint', ''))
        else:
            arr_posbits = 0
        input_bitdepth_pos = max(input_bitdepth_pos, arr_posbits)

    # If maximum positive bitdepth from inputs is too low,
    # cast structure to minimum positive bitdepth for conovlution.
    if input_bitdepth_pos < conv_bitdepth_pos:
        if conv_bitdepth_pos != 1 and (conv_bitdepth_pos % 2) != 0:
            conv_bitdepth_pos += 1
        structure = structure.astype(eval('np.uint{}'.format(conv_bitdepth_pos)))

    return structure


def imerode_slow(array, nhood, iterations=1, mode='auto',
                 cast_structure_for_speed=True, allow_flipped_processing=True):
    """
    Erode an array with the provided binary structure.

    Parameters
    ----------
    array : ndarray, 2D
        Array to erode.
    nhood : positive int, tuple like `array.shape`, or (ndarray, 2D)
        If an integer / tuple, specifies the side length / shape
        of structure (of ones) to be used as structure for erosion.
        If ndarray, must be a binary array with True/1-valued elements
        specifying the structure for erosion.
    iterations : positive int
        Number of times to perform the erosion.
    mode : str; 'auto', 'conv', 'skimage', 'scipy', or 'scipy_grey'
        Specifies which method will be used to perform erosion.
        'auto' -------- use the fastest of ('conv', 'scipy') given array, structure sizes
        'conv' -------- `conv2`
        'skimage' ----- `skimage.morphology.binary_erosion` [1]
        'scipy' ------- `scipy.ndimage.binary_erosion` [2]
        'scipy_grey' -- `scipy.ndimage.grey_erosion` [3]
    cast_structure_for_speed : bool
        If True and `structure` is not float32 data type, cast it to float32.
        This produces the fastest results overall for all methods,
        and for 'conv' method this prevents a potential fallback call to
        `conv2_slow` if input structure has an unsupported data type for
        fast OpenCV method used in `conv2`.
    allow_flipped_processing : bool
        If True and at least one of `structure`'s side lengths is even,
        rotate both `array` `structure` 180 degrees before performing erosion,
        then rotate the result array 180 degrees before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [4].

    Returns
    -------
    imerode_slow : ndarray, same shape and type as `array`
        Array containing the erosion of the input array by the structure.

    See Also
    --------
    imdilate_slow

    Notes
    -----
    This function is meant to replicate MATLAB's `imerode` function [4].

    Strictly binary erosion will be performed if and only if `array.dtype` is `np.bool`,
    otherwise greyscale erosion will be performed. However, greyscale erosion on a
    binary array containing only values X and Y produces the same result as if the
    values [min(X, Y), max(X, Y)] were mapped to [0, 1] and cast to a boolean array,
    passed into this function, then mapped values in the result array back to their
    original values (for floating `array`, note `-inf < 0 < inf < NaN`).

    All modes will handle greyscale erosion when `array` is not boolean.
    For `array` of feasibly large sizes containing more than two values,
    'scipy_grey' is the fastest method for performing greyscale erosion,
    but since the method may interpolate on the boundaries between regions
    of differing values (which the MATLAB function does not do), it is not
    an acceptable default method and is not considered when `mode=='auto'`.

    In preliminary testing, all three methods 'conv', 'scipy', and 'skimage'
    are able to reproduce the results of the MATLAB function for both binary
    and greyscale erosion (with the exception of some edge pixels when
    `structure` with a False/zero center element is used in grey erosion,
    which produces nonsensical values where proper erosion cannot be detected
    by these three methods as well as MATLAB's function -- only the 'scipy_grey'
    method handles this case properly).

    References
    ----------
    .. [1] http://scikit-image.org/docs/dev/api/skimage.morphology.html#skimage.morphology.binary_erosion
    .. [2] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.ndimage.morphology.binary_erosion.html
    .. [3] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.ndimage.morphology.grey_erosion.html
    .. [4] https://www.mathworks.com/help/images/ref/imerode.html

    """
    mode_choices = ('auto', 'conv', 'skimage', 'scipy', 'scipy_grey')

    structure = None
    if type(nhood) == int:
        structure = np.ones((nhood, nhood), dtype=np.float32)
    elif type(nhood) == tuple:
        structure = np.ones(nhood, dtype=np.float32)
    elif type(nhood) == np.ndarray:
        structure = nhood
        if structure.dtype != np.bool and np.any(~np.logical_or(structure == 0, structure == 1)):
            raise InvalidArgumentError("`nhood` structure contains values other than 0 and 1")
        if cast_structure_for_speed and structure.dtype != np.float32:
            structure = structure.astype(np.float32)
    else:
        raise InvalidArgumentError("`nhood` type may only be int, tuple, or ndarray, "
                                   "but was {} (nhood={})".format(type(nhood), nhood))

    if mode not in mode_choices:
        raise UnsupportedMethodError("'mode' must be one of {}, but was '{}'".format(mode_choices, mode))

    if mode == 'auto':
        # FIXME: Get new time coefficients for faster conv2 function now being used.
        # Make an estimate of the runtime for 'conv' and 'scipy' methods,
        # then choose the faster method.
        array_elements = np.prod(array.shape)
        struc_elements = np.prod(structure.shape)
        time_conv = 1.25e-07 * array_elements - 7.61e-02
        time_scipy = (  (1.56e-10 * array_elements - 2.66e-04) * struc_elements
                      + (1.34e-08 * array_elements - 1.42e-02) )
        mode = 'conv' if time_conv < time_scipy else 'scipy'

    if mode == 'conv':
        # Uncomment the following if conv2_slow function is used.
        if (    not isinstance(structure.dtype.type(1), np.floating)
            and not isinstance(array.dtype.type(1), np.floating) ):
            # Make sure one of the input integer arrays has great enough
            # positive bitdepth to prevent overflow during convolution.
            structure = conv_binary_structure_prevent_overflow(array, structure)
        structure = np.rot90(structure, 2)

    rotation_flag = False
    if allow_flipped_processing:
        array, structure, rotation_flag = rotate_arrays_if_kernel_has_even_sidelength(array, structure)

    if mode == 'skimage':
        pady, padx = np.array(structure.shape) / 2
        pady, padx = int(pady), int(padx)
        if array.dtype == np.bool:
            padval = 1
        else:
            padval = np.inf if isinstance(array.dtype.type(1), np.floating) else np.iinfo(array.dtype).max
        array = np.pad(array, ((pady, pady), (padx, padx)), 'constant', constant_values=padval)

    for i in range(iterations):

        if array.dtype == np.bool:
            # Binary erosion
            if mode == 'conv':
                result = (conv2(~array, structure, shape='same', allow_flipped_processing=False) == 0)
            elif mode in ('scipy', 'scipy_grey'):
                result = sp_ndimage.binary_erosion(array, structure, border_value=1)
            elif mode == 'skimage':
                result = sk_morphology.binary_erosion(array, structure)

        elif mode == 'scipy_grey':
            # Greyscale erosion
            if np.any(structure != 1):
                if not isinstance(structure.dtype.type(1), np.floating):
                    structure = structure.astype(np.float32)
                result = sp_ndimage.grey_erosion(array, structure=(structure - 1))
            else:
                result = sp_ndimage.grey_erosion(array, size=structure.shape)

        else:
            # Greyscale erosion
            array_vals = np.unique(array)
            if isinstance(array.dtype.type(1), np.floating):
                array_vals_nans = np.isnan(array_vals)
                has_nans = np.any(array_vals_nans)
                if has_nans:
                    array_nans = np.isnan(array)
                    # Remove possible multiple occurrences of "nan" in results of np.unique().
                    array_vals = np.delete(array_vals, np.where(np.isnan(array_vals)))
                    array_vals = np.append(array_vals, np.nan)
            else:
                has_nans = False

            # Start with an array full of the lowest value from the input array.
            # Overlay the erosion of all higher-value layers (combined)
            # as the second-lowest value. Call this the new lowest value,
            # and repeat until all layers have been added up through the highest value.
            result = np.full_like(array, array_vals[0])
            for val in array_vals[1:]:
                if not np.isnan(val):
                    mask_val = (array >= val) if not has_nans else np.logical_or(array >= val, array_nans)
                else:
                    mask_val = array_nans if mode != 'skimage' else np.logical_or(array_nans, array == np.inf)

                if mode == 'conv':
                    result_val = (conv2(~mask_val, structure, shape='same', allow_flipped_processing=False) == 0)
                elif mode == 'scipy':
                    result_val = sp_ndimage.binary_erosion(mask_val, structure, border_value=1)
                elif mode == 'skimage':
                    result_val = sk_morphology.binary_erosion(mask_val, structure)

                result[result_val] = val

        array = result

    if mode == 'skimage':
        result = result[pady:-pady, padx:-padx]

    return fix_array_if_rotation_was_applied(result, rotation_flag)


def imdilate_slow(array, nhood, iterations=1, mode='auto',
                  cast_structure_for_speed=True, allow_flipped_processing=True):
    """
    Dilate an array with the provided binary structure.

    Parameters
    ----------
    array : ndarray, 2D
        Array to dilate.
    nhood : positive int, tuple like `array.shape`, or (ndarray, 2D)
        If an integer / tuple, specifies the side length / shape
        of structure (of ones) to be used as structure for dilation.
        If ndarray, must be a binary array with True/1-valued elements
        specifying the structure for dilation.
    iterations : positive int
        Number of times to perform the dilation.
    mode : str; 'auto', 'conv', 'skimage', 'scipy', or 'scipy_grey'
        Specifies which method will be used to perform dilation.
        'auto' -------- use the fastest of ('conv', 'scipy') given array, structure sizes
        'conv' -------- `conv2`
        'skimage' ----- `skimage.morphology.binary_dilation` [1]
        'scipy' ------- `scipy.ndimage.binary_dilation` [2]
        'scipy_grey' -- `scipy.ndimage.grey_dilation` [3]
    cast_structure_for_speed : bool
        If True and `structure` is not float32 data type, cast it to float32.
        This produces the fastest results overall for all methods,
        and for 'conv' method this prevents a potential fallback call to
        `conv2_slow` if input structure has an unsupported data type for
        fast OpenCV method used in `conv2`.
    allow_flipped_processing : bool
        If True and at least one of `structure`'s side lengths is even,
        rotate both `array` `structure` 180 degrees before performing dilation,
        then rotate the result array 180 degrees before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [4].

    Returns
    -------
    imdilate_slow : ndarray, same shape and type as `array`
        Array containing the dilation of the input array by the structure.

    See Also
    --------
    imerode_slow

    Notes
    -----
    This function is meant to replicate MATLAB's `imdilate` function [4].

    Strictly binary dilation will be performed if and only if `array.dtype` is `np.bool`,
    otherwise greyscale dilation will be performed. However, greyscale dilation on a
    binary array containing only values X and Y produces the same result as if the
    values [min(X, Y), max(X, Y)] were mapped to [0, 1] and cast to a boolean array,
    passed into this function, then mapped values in the result array back to their
    original values (for floating `array`, note `-inf < 0 < inf < NaN`).

    All modes will handle greyscale dilation when `array` is not boolean.
    For `array` of feasibly large sizes containing more than two values,
    'scipy_grey' is the fastest method for performing greyscale dilation,
    but since the method may interpolate on the boundaries between regions
    of differing values (which the MATLAB function does not do), it is not
    an acceptable default method and is not considered when `mode=='auto'`.

    In preliminary testing, all three methods 'conv', 'scipy', and 'skimage'
    are able to reproduce the results of the MATLAB function for both binary
    and greyscale dilation (with the exception of some edge pixels when
    `structure` with a False/zero center element is used in grey dilation,
    which produces nonsensical values where proper dilation cannot be detected
    by these three methods as well as MATLAB's function -- only the 'scipy_grey'
    method handles this case properly).

    References
    ----------
    .. [1] http://scikit-image.org/docs/dev/api/skimage.morphology.html#skimage.morphology.binary_dilation
    .. [2] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.ndimage.morphology.binary_dilation.html
    .. [3] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.ndimage.morphology.grey_dilation.html
    .. [4] https://www.mathworks.com/help/images/ref/imdilate.html

    """
    mode_choices = ('auto', 'conv', 'skimage', 'scipy', 'scipy_grey')

    structure = None
    if type(nhood) == int:
        structure = np.ones((nhood, nhood), dtype=np.float32)
    elif type(nhood) == tuple:
        structure = np.ones(nhood, dtype=np.float32)
    elif type(nhood) == np.ndarray:
        structure = nhood
        if structure.dtype != np.bool and np.any(~np.logical_or(structure == 0, structure == 1)):
            raise InvalidArgumentError("`nhood` structure contains values other than 0 and 1")
        if cast_structure_for_speed and structure.dtype != np.float32:
            structure = structure.astype(np.float32)
    else:
        raise InvalidArgumentError("`nhood` type may only be int, tuple, or ndarray, "
                                   "but was {} (nhood={})".format(type(nhood), nhood))

    if mode not in mode_choices:
        raise UnsupportedMethodError("'mode' must be one of {}, but was '{}'".format(mode_choices, mode))

    if mode == 'auto':
        # FIXME: Get new time coefficients for faster conv2 function now being used.
        # Make an estimate of the runtime for 'conv' and 'scipy' methods,
        # then choose the faster method.
        array_elements = np.prod(array.shape)
        struc_elements = np.prod(structure.shape)
        time_conv = 1.23e-07 * array_elements - 4.62e-02
        time_scipy = (  (6.60e-10 * array_elements - 3.59e-04) * struc_elements
                      + (2.43e-08 * array_elements + 4.05e-02) )
        mode = 'conv' if time_conv < time_scipy else 'scipy'

    if mode == 'conv':
        if (    not isinstance(structure.dtype.type(1), np.floating)
            and not isinstance(array.dtype.type(1), np.floating) ):
            # Make sure one of the input integer arrays has great enough
            # positive bitdepth to prevent overflow during convolution.
            structure = conv_binary_structure_prevent_overflow(array, structure)

    rotation_flag = False
    if mode in ('scipy', 'scipy_grey', 'skimage') and allow_flipped_processing:
        array, structure, rotation_flag = rotate_arrays_if_kernel_has_even_sidelength(array, structure)

    for i in range(iterations):

        if array.dtype == np.bool:
            # Binary dilation
            if mode == 'conv':
                result = (conv2(array, structure, shape='same', allow_flipped_processing=False) > 0)
            elif mode in ('scipy', 'scipy_grey'):
                result = sp_ndimage.binary_dilation(array, structure, border_value=0)
            elif mode == 'skimage':
                result = sk_morphology.binary_dilation(array, structure)

        elif mode == 'scipy_grey':
            # Greyscale dilation
            if np.any(structure != 1):
                if not isinstance(structure.dtype.type(1), np.floating):
                    structure = structure.astype(np.float32)
                result = sp_ndimage.grey_dilation(array, structure=(structure - 1))
            else:
                result = sp_ndimage.grey_dilation(array, size=structure.shape)

        else:
            # Greyscale dilation
            array_vals = np.unique(array)
            if isinstance(array.dtype.type(1), np.floating):
                array_vals_nans = np.isnan(array_vals)
                has_nans = np.any(array_vals_nans)
                if has_nans:
                    # Remove possible multiple occurrences of "nan" in results of np.unique().
                    array_vals = np.delete(array_vals, np.where(np.isnan(array_vals)))
                    array_vals = np.append(array_vals, np.nan)

            # Start with an array full of the lowest value from the input array,
            # then overlay the dilation of each higher-value layer,
            # one at a time, until all layers have been added.
            result = np.full_like(array, array_vals[0])
            for val in array_vals[1:]:
                mask_val = (array == val) if not np.isnan(val) else np.isnan(array)

                if mode == 'conv':
                    result_val = (conv2(mask_val, structure, shape='same', allow_flipped_processing=False) > 0)
                elif mode == 'scipy':
                    result_val = sp_ndimage.binary_dilation(mask_val, structure, border_value=0)
                elif mode == 'skimage':
                    result_val = sk_morphology.binary_dilation(mask_val, structure)

                result[result_val] = val

        array = result

    return fix_array_if_rotation_was_applied(result, rotation_flag)


def imerode(array, nhood, iterations=1, allow_flipped_processing=True):
    """
    Erode an array with the provided binary structure.

    See documentation for `imerode_imdilate_cv2`.

    """
    return imerode_imdilate_cv2(array, nhood, iterations, allow_flipped_processing, erode=True)


def imdilate(array, nhood, iterations=1, allow_flipped_processing=True):
    """
    Dilate an array with the provided binary structure.

    See documentation for `imerode_imdilate_cv2`.

    """
    return imerode_imdilate_cv2(array, nhood, iterations, allow_flipped_processing, erode=False)


def imerode_imdilate_cv2(array, nhood, iterations=1,
                         allow_flipped_processing=True, erode=True):
    """
    Erode/Dilate an array with the provided binary structure.

    Parameters
    ----------
    array : ndarray, 2D
        Array to erode/dilate.
    nhood : positive int, tuple like `array.shape`, or (ndarray, 2D)
        If an integer / tuple, specifies the side length / shape
        of structure (of ones) to be used as structure for erosion/dilation.
        If ndarray, must be a binary array with True/1-valued elements
        specifying the structure for erosion/dilation.
    iterations : positive int
        Number of times to perform the erosion/dilation.
    allow_flipped_processing : bool
        If True and at least one of `structure`'s side lengths is even,
        rotate both `array` `structure` 180 degrees before performing erosion/dilation,
        then rotate the result array 180 degrees before returning.
        The sole purpose of this option is to allow this function
        to most closely replicate the corresponding MATLAB array method [3,4].
    erode : bool
        If True, perform erosion.
        If False, perform dilation.

    Returns
    -------
    imerode_imdilate_cv2 : ndarray, same shape and type as `array`
        Array containing the erosion/dilation of the input array by the structure.

    Notes
    -----
    This wrapper function for OpenCV's `erode`/`dilate` function [1,2] is meant to replicate
    MATLAB's `imerode`/`imdilate` function [3,4].

    In preliminary testing, this method reproduces results of the MATLAB function
    for both binary and greyscale erosion/dilation, with the exception of some edge pixels
    when `structure` with a False/zero center element is used in grey erosion/dilation,
    which produces nonsensical values where proper erosion/dilation cannot be detected
    by this method as well as MATLAB's function.

    References
    ----------
    .. [1] https://docs.opencv.org/2.4/modules/imgproc/doc/filtering.html#erode
    .. [1] https://docs.opencv.org/2.4/modules/imgproc/doc/filtering.html#dilate
    .. [3] https://www.mathworks.com/help/images/ref/imerode.html
    .. [3] https://www.mathworks.com/help/images/ref/imdilate.html

    """
    structure = None
    if type(nhood) == int:
        structure = np.ones((nhood, nhood), dtype=np.uint8)
    elif type(nhood) == tuple:
        structure = np.ones(nhood, dtype=np.uint8)
    elif type(nhood) == np.ndarray:
        structure = nhood
        if structure.dtype != np.bool and np.any(~np.logical_or(structure == 0, structure == 1)):
            raise InvalidArgumentError("`nhood` structure contains values other than 0 and 1")
    else:
        raise InvalidArgumentError("`nhood` type may only be int, tuple, or ndarray, "
                                   "but was {} (nhood={})".format(type(nhood), nhood))

    cv2_dtypes = [np.uint8, np.int16, np.uint16, np.float32, np.float64]

    # Check array data type.
    array_dtype_in = array.dtype
    if array_dtype_in not in cv2_dtypes:
        dtype_errmsg = ("Fast erosion/dilation method only allows array dtypes {}, "
                        "but was {}".format([str(d(1).dtype) for d in cv2_dtypes], array_dtype_in))

        # Only cast to a higher data type for safety.
        array_dtype_cast = None
        if array_dtype_in == np.bool:
            array_dtype_cast = np.uint8
        elif array_dtype_in == np.int8:
            array_dtype_cast = np.int16
        elif array_dtype_in == np.float16:
            array_dtype_cast = np.float32

        if array_dtype_cast is not None:
            # warn(dtype_errmsg + "\n-> Casting array from {} to {} for processing".format(
            #      array_dtype_in, array_dtype_cast(1).dtype))
            array = array.astype(array_dtype_cast)

        if array_dtype_cast is None:
            # Fall back to old (slower) imdilate/imerode functions.
            warn(dtype_errmsg + "\n-> Falling back to slower methods")
            fn = imerode_slow if erode else imdilate_slow
            return fn(array, nhood, iterations, allow_flipped_processing=allow_flipped_processing)

    # Check structure data type.
    if structure.dtype != np.uint8:
        warn("Fast erosion/dilation method only allows structure dtype np.uint8, but was {}".format(structure.dtype)
             + "\n-> Casting structure from {} to uint8".format(structure.dtype))
        structure = structure.astype(np.uint8)

    rotation_flag = False
    if erode:
        # Erosion settings
        fn = cv2.erode
        if allow_flipped_processing:
            array, structure, rotation_flag = rotate_arrays_if_kernel_has_even_sidelength(array, structure)
    else:
        # Dilation settings
        fn = cv2.dilate
        structure = np.rot90(structure, 2)

    # Perform erosion/dilation.
    result = fn(array, structure, iterations=iterations, borderType=cv2.BORDER_REPLICATE)
    if result.dtype != array_dtype_in:
        result = result.astype(array_dtype_in)

    return fix_array_if_rotation_was_applied(result, rotation_flag)


def bwareaopen(array, size_tolerance, connectivity=8, in_place=False):
    """
    Remove connected components smaller than the specified size.

    This is a wrapper function for Scikit-Image's `morphology.remove_small_objects` [1]
    meant to replicate MATLAB's `bwareaopen` function [2] for boolean input `array`.

    Parameters
    ----------
    See documentation for `skimage.morphology.remove_small_objects` [1], where...
    array : ndarray, 2D
        Equivalent to `ar`.
    size_tolerance : positive int
        Equivalent to `min_size`.
    connectivity : int, 4 or 8
        For drawing boundaries...
        If 4, only pixels with touching edges are considered connected.
        If 8, pixels with touching edges and corners are considered connected.
    in_place : bool
        Equivalent to `in_place`.

    Returns
    -------
    bwareaopen : ndarray, same shape and type as `array`
        The input array with small connected components removed.

    References
    ----------
    .. [1] http://scikit-image.org/docs/dev/api/skimage.morphology.html#skimage.morphology.remove_small_objects

    """
    return sk_morphology.remove_small_objects(array, size_tolerance, connectivity/4, in_place)


def bwboundaries_array(array, side='inner', connectivity=8, noholes=False,
                       grey_boundaries=False, edge_boundaries=True):
    """
    Return an array with 1-pixel-wide lines (borders) highlighting
    boundaries between areas of differing values in the input array.

    Parameters
    ----------
    array : ndarray, 2D
        Array from which to extract data value boundaries.
    side : str, 'inner' or 'outer'
        Between areas of different values in `array`...
        If 'inner', boundaries are drawn on the side of the higher value.
        If 'outer', boundaries are drawn on the side of the lower value.
    connectivity : int, 4 or 8
        For drawing boundaries...
        If 4, only pixels with touching edges are considered connected.
        If 8, pixels with touching edges and corners are considered connected.
    noholes : bool
        (Option only applies for boolean `array`.)
        If True, do not draw boundaries of zero clusters surrounded by ones.
    grey_boundaries : bool
        If True and a non-boolean array is provided,
        boundary pixels in the result array are assigned the same value
        as their location counterparts in `array`.
        Thus, the value a particular section of boundary border takes on
        is determined by `side`. Additionally, if `side='inner'/'outer'`,
        the fill value between boundaries is the minimum/maximum value of
        `array`, respectively.
        If False, return a boolean array with True-valued pixels
        highlighting only the location of all boundaries.
    edge_boundaries : bool
        If True, copy the values of all edge pixels in `array` to the
        result array.

    Returns
    -------
    bwboundaries_array : ndarray of bool, same shape as `array`
        A binary array with 1-px borders of ones highlighting boundaries
        between areas of differing values in the input array.

    See Also
    --------
    imerode
    imdilate

    Notes
    -----
    This function utilizes local `imerode` and `imdilate` functions
    as a means to replicate MATLAB's `bwboundaries` function [1].

    References
    ----------
    .. [1] https://www.mathworks.com/help/images/ref/bwboundaries.html

    """
    side_choices = ('inner', 'outer')
    conn_choices = (4, 8)

    if side not in side_choices:
        raise InvalidArgumentError("`side` must be one of {}, but was '{}'".format(side_choices, side))
    if connectivity not in conn_choices:
        raise InvalidArgumentError("`connectivity` must be one of {}, but was {}".format(conn_choices, connectivity))

    structure = np.zeros((3, 3), dtype=np.uint8)
    if connectivity == 8:
        structure[:, 1] = 1
        structure[1, :] = 1
    elif connectivity == 4:
        structure[:, :] = 1

    if noholes:
        array = sp_ndimage.binary_fill_holes(array)

    fn = imerode if side == 'inner' else imdilate

    # Find boundaries.
    array_boundaries = (array != fn(array, structure))

    if grey_boundaries and array.dtype != np.bool:
        fillval = np.nanmin(array) if side == 'inner' else np.max(array)
        result = np.full_like(array, fillval)
        result[array_boundaries] = array[array_boundaries]
    else:
        result = array_boundaries

    # Edge pixels may not be marked as boundary pixels
    # by erosion or dilation, and must be added manually.
    if edge_boundaries:
        result[ 0,  :] = array[ 0,  :]
        result[-1,  :] = array[-1,  :]
        result[ :,  0] = array[ :,  0]
        result[ :, -1] = array[ :, -1]

    return result


def entropyfilt(array, nhood=np.ones((9,9),dtype=np.uint8), bin_bitdepth=8, nbins=None,
                scale_from='dtype_max', symmetric_border=True, allow_modify_array=False):
    """
    Calculate local entropy of a grayscale image.

    If the numerical range of data in `array` is greater than the
    provided maximum number of bins (through either `nbins` or
    `bin_bitdepth`), data values are scaled down to fit within the
    number of bins (as a range of continuous integers) and cast to an
    integer data type before entropy calculation.
    If `array` data type is floating, values are rounded and cast to
    an integer data type regardless, but (if `scale_from='array_range'`)
    no pre-scaling is applied if the input data range is within the
    maximum number of bins.

    Parameters
    ----------
    array : ndarray, 2D
        Array for which to calculate local entropy.
    nhood : positive int, tuple like `array.shape`, or (ndarray, 2D)
        If an integer / tuple, specifies the side length / shape
        of structure (of ones) to be used as structure for filter.
        If ndarray, must be a binary array with True/1-valued elements
        specifying the structure for filter.
    bin_bitdepth : None or `1 <= int <= 16`
        Scale `array` data to fit in `2^bin_bitdepth` bins for
        entropy calculation if range of values is greater than
        number of bins.
        If None, `nbins` must be provided and this is set by
        `bin_bitdepth = math.log(nbins, 2)`.
    nbins : None or `2 <= int <= 2^16`
        (If not None, overrides `bin_bitdepth`)
        Scale `array` data to fit in `nbins` bins for entropy
        calculation if necessary. for entropy
        calculation if range of values is greater than number
        of bins.
        If None, `bin_bitdepth` must be provided.
    scale_from : str; 'dtype_max' or 'array_range'
        If 'dtype_max' and bitdepth of `array` data type is
        greater than `bin_bitdepth`, scale array data to fit
        in `nbins` bins by first dividing array values by the
        maximum possible value for the input array data type
        before multiplying by `nbins`.
        If 'array_range' and the range of values in `array` is
        greater than `nbins`, scale array data by translating
        the minimum array value to zero then dividing values
        by the maximum array value before multiplying by `nbins`.
    symmetric_border : bool
        If True, pads `array` edges with the reflections of
        each edge so that `kernel` picks up these values when
        it hangs off the edges of `array` during entropy
        calculations. Mimics MATLAB's `entropyfilt` function [2].
        If False, only values within the bounds of `array` are
        considered during entropy calculations.
    allow_modify_array : bool
        (Option only applies for floating `array`.)
        Allow modifying values in `array` to save some memory
        allocation in the case that rounding of data values is
        performed on the input array itself.

    Returns
    -------
    entropyfilt : ndarray of float64, same shape as `array`
        Array containing the entropy-filtered image.

    Notes
    -----
    This function utilizes Scikit-Image's `filters.rank.entropy`
    function [1] as a means to replicate MATLAB's `entropyfilt`
    function [2].
    Kernel-wise entropy calculations are done as described in
    MATLAB's documentation for its `entropy` function [3].

    Scikit-Image's entropy function accepts only uint8 and uint16
    arrays, but since it appears uint16 processes faster than
    uint8, array copy is cast to uint16 before being sent in.

    References
    ----------
    .. [1] http://scikit-image.org/docs/dev/api/skimage.filters.rank.html?highlight=entropy#skimage.filters.rank.entropy
           http://scikit-image.org/docs/dev/auto_examples/filters/plot_entropy.html
    .. [2] https://www.mathworks.com/help/images/ref/entropyfilt.html
    .. [3] https://www.mathworks.com/help/images/ref/entropy.html

    """
    structure = None
    if type(nhood) == int:
        structure = np.ones((nhood, nhood), dtype=np.uint8)
    elif type(nhood) == tuple:
        structure = np.ones(nhood, dtype=np.uint8)
    elif type(nhood) == np.ndarray:
        structure = nhood
        if structure.dtype != np.bool and np.any(~np.logical_or(structure == 0, structure == 1)):
            raise InvalidArgumentError("`nhood` structure contains values other than 0 and 1")
    else:
        raise InvalidArgumentError("`nhood` type may only be int, tuple, or ndarray, "
                                   "but was {} (nhood={})".format(type(nhood), nhood))

    if bin_bitdepth is None and nbins is None:
        raise InvalidArgumentError("Either `bin_bitdepth` or `nbins` must be provided")
    if nbins is None:
        if type(bin_bitdepth) == int and 1 <= bin_bitdepth <= 16:
            nbins = 2**bin_bitdepth
        else:
            raise InvalidArgumentError("`bin_bitdepth` must be an integer between 1 and 16, inclusive, "
                                       "but was {}".format(bin_bitdepth))
    else:
        if type(nbins) == int and 2 <= nbins <= 2**16:
            bin_bitdepth = math.log(nbins, 2)
        else:
            raise InvalidArgumentError("`nbins` must be an integer between 2 and 2**16, inclusive, "
                                       "but was {}".format(nbins))

    # Check array data type.
    array_backup = array
    array_dtype_in = array.dtype
    array_dtype_bitdepth = None
    array_dtype_max = None
    array_dtype_unsigned = False
    if array_dtype_in == np.bool:
        array_dtype_bitdepth = 1
        array_dtype_max = 1
    if isinstance(array_dtype_in.type(1), np.integer):
        array_dtype_bitdepth = int(str(array_dtype_in).split('int')[-1])
        array_dtype_max = np.iinfo(array_dtype_in).max
        if array_dtype_in.kind == 'u':
            array_dtype_unsigned = True
    elif isinstance(array_dtype_in.type(1), np.floating):
        array_dtype_bitdepth = np.inf
        array_dtype_max = np.finfo(array_dtype_in).max
    else:
        raise UnsupportedDataTypeError("array dtype {} is not supported".format(array_dtype_in))

    # Create scaled-down version of array according
    # to input bin_bitdepth or number of bins nbins.
    if nbins is None:
        nbins = 2**bin_bitdepth

    if scale_from == 'dtype_max' and not array_dtype_unsigned:
        # For signed array data types, bin_array_max is a one-sided limit.
        # For even values of nbins, let nbins be decreased by one to accomodate.
        if nbins == 2:
            raise InvalidArgumentError("`nbins` must be >= 3 for signed `array` data type "
                                       "when scale_from='dtype_max'")
        bin_array_max = int(np.ceil(nbins/2) - 1)
    else:
        bin_array_max = nbins - 1

    bin_array = None
    if array_dtype_bitdepth <= bin_bitdepth:
        bin_array = array

    elif scale_from == 'dtype_max':
        if not isinstance(array_dtype_in.type(1), np.floating):
            array = array.astype(np.float32) if array_dtype_bitdepth <= 16 else array.astype(np.float64)
        bin_array = array / array_dtype_max * bin_array_max

    elif scale_from == 'array_range':
        array_min = np.nanmin(array)
        array_max = np.nanmax(array)
        array_range = array_max - array_min

        if array_range < nbins:
            if array_min >= 0 and array_max <= np.iinfo(np.uint16).max:
                bin_array = array
            else:
                # Since only value *counts*, not numerical values themselves,
                # matter to entropy filter, shift array values so that minimum
                # is set to zero.
                if isinstance(array_dtype_in.type(1), np.floating):
                    array = array_round_proper(array, allow_modify_array)
                bin_array = np.empty_like(array, np.uint16)
                np.subtract(array, array_min, out=bin_array, casting='unsafe')
        else:
            # Shift array values so that minimum is set to zero,
            # then scale to maximum number of bins.
            if not isinstance(array_dtype_in.type(1), np.floating):
                array = array.astype(np.float32) if array_dtype_bitdepth <= 16 else array.astype(np.float64)
            bin_array = (array - array_min) / array_range * bin_array_max

    # Convert bin array to uint16.
    # This is to both catch integer/floating arrays and
    # cast them to an acceptable data type for `entropy`
    # function, and because it appears uint16 processes
    # faster than uint8.
    if bin_array.dtype != np.uint16:
        if isinstance(bin_array.dtype.type(1), np.floating):
            if bin_array is not array_backup:
                allow_modify_array = True
            bin_array = array_round_proper(bin_array, allow_modify_array)
        bin_array = bin_array.astype(np.uint16)

    # Edge settings
    if symmetric_border:
        pady_top, padx_lft = (np.array(structure.shape) - 1) / 2
        pady_bot, padx_rht = np.array(structure.shape) / 2
        pady_top, padx_lft = int(pady_top), int(padx_lft)
        pady_bot, padx_rht = int(pady_bot), int(padx_rht)
        bin_array = np.pad(bin_array, ((pady_top, pady_bot), (padx_lft, padx_rht)), 'symmetric')

    # Perform entropy filter.
    result = entropy(bin_array, structure)

    # Crop result if necessary.
    if symmetric_border:
        pady_bot = -pady_bot if pady_bot != 0 else None
        padx_rht = -padx_rht if padx_rht != 0 else None
        result = result[pady_top:pady_bot, padx_lft:padx_rht]

    return result


def convex_hull_image_offsets_diamond(ndim):
    # TODO: Continue to update this fork of skimage function until
    # -t    the skimage version includes fast polygon_perimeter function.
    offsets = np.zeros((2 * ndim, ndim))
    for vertex, (axis, offset) in enumerate(product(range(ndim), (-0.5, 0.5))):
        offsets[vertex, axis] = offset
    return offsets


def convex_hull_image(image, offset_coordinates=True, tolerance=1e-10):
    # TODO: Continue to update this fork of skimage function until
    # -t    the skimage version includes fast polygon_perimeter function.
    """Compute the convex hull image of a binary image.
    The convex hull is the set of pixels included in the smallest convex
    polygon that surround all white pixels in the input image.

    Parameters
    ----------
    image : array
        Binary input image. This array is cast to bool before processing.
    offset_coordinates : bool, optional
        If ``True``, a pixel at coordinate, e.g., (4, 7) will be represented
        by coordinates (3.5, 7), (4.5, 7), (4, 6.5), and (4, 7.5). This adds
        some "extent" to a pixel when computing the hull.
    tolerance : float, optional
        Tolerance when determining whether a point is inside the hull. Due
        to numerical floating point errors, a tolerance of 0 can result in
        some points erroneously being classified as being outside the hull.

    Returns
    -------
    hull : (M, N) array of bool
        Binary image with pixels in convex hull set to True.

    References
    ----------
    .. [1] http://blogs.mathworks.com/steve/2011/10/04/binary-image-convex-hull-algorithm-notes/

    """
    ndim = image.ndim

    # In 2D, we do an optimisation by choosing only pixels that are
    # the starting or ending pixel of a row or column.  This vastly
    # limits the number of coordinates to examine for the virtual hull.
    if ndim == 2:
        coords = sk_morphology._convex_hull.possible_hull(image.astype(np.uint8))
    else:
        coords = np.transpose(np.nonzero(image))
        if offset_coordinates:
            # when offsetting, we multiply number of vertices by 2 * ndim.
            # therefore, we reduce the number of coordinates by using a
            # convex hull on the original set, before offsetting.
            hull0 = scipy.spatial.ConvexHull(coords)
            coords = hull0.points[hull0.vertices]

    # Add a vertex for the middle of each pixel edge
    if offset_coordinates:
        offsets = convex_hull_image_offsets_diamond(image.ndim)
        coords = (coords[:, np.newaxis, :] + offsets).reshape(-1, ndim)

    # ERIK'S NOTE: Added the following conditional barrier for speed.
    if offset_coordinates or ndim != 2:
        # repeated coordinates can *sometimes* cause problems in
        # scipy.spatial.ConvexHull, so we remove them.
        coords = unique_rows(coords)

    # Find the convex hull
    hull = ConvexHull(coords)
    vertices = hull.points[hull.vertices]

    # If 2D, use fast Cython function to locate convex hull pixels
    if ndim == 2:
        # ERIK'S NOTE: Substituted grid_points_in_poly() for the following for speed.
        # mask = grid_points_in_poly(image.shape, vertices)
        hull_perim_r, hull_perim_c = polygon_perimeter(vertices[:, 0], vertices[:, 1])
        mask = np.zeros(image.shape, dtype=np.bool)
        mask[hull_perim_r, hull_perim_c] = True
        mask = sp_ndimage.morphology.binary_fill_holes(mask)
    else:
        gridcoords = np.reshape(np.mgrid[tuple(map(slice, image.shape))],
                                (ndim, -1))
        # A point is in the hull if it satisfies all of the hull's inequalities
        coords_in_hull = np.all(hull.equations[:, :ndim].dot(gridcoords) +
                                hull.equations[:, ndim:] < tolerance, axis=0)
        mask = np.reshape(coords_in_hull, image.shape)

    return mask


def concave_hull_traverse_delaunay(boundary_points, convex_hull, vertex_neighbor_vertices,
                                   boundary_res=0):
    """
    Traverse paths for convex hull edge erosion to obtain
    information necessary for computing the concave hull image.

    Triangle edges in the input Delaunay triangulation that are
    considered for erosion are cataloged with their edge length
    and critical value of erosion tolerance, information to be
    used by `concave_hull_image`.

    Parameters
    ----------
    boundary_points : ndarray of float, shape (npoints, 2)
        Coordinates of all data (non-zero) pixels in the original
        image from which the concave hull image is being extracted.
        This must be identical to the source coordinates for the
        Delaunay triangulation from which `convex_hull` and
        `vertex_neighbor_vertices` are derived [1].
    convex_hull : ndarray of int, shape (nedges, 2) [2]
        Vertices of facets forming the convex hull of the point set.
        Each element contains a set of indices into `boundary_points`
        used to retrieve coordinates for convex hull edge endpoints.
        This must be derived from the same Delaunay triangulation
        as `vertex_neighbor_vertices`.
    vertex_neighbor_vertices : tuple of two ndarrays of int (indices, indptr) [3]
        Used to determine neighboring vertices of vertices.
        The indices of neighboring vertices of vertex k are
        `indptr[indices[k]:indices[k+1]]`.
        This must be derived from the same Delaunay triangulation
        as `convex_hull`.
    boundary_res : positive int
        Minimum x or y *coordinate-wise* distance between two points
        in a triangle for their edge to be traversed, thereby allowing
        the triangle on the other side of that edge to be considered
        for erosion.
        If there are regions in the triangulation associated with
        a particular minimum point density whose boundaries should
        not be breached by erosion (such as regions of "good data"
        points taken from an image that have a regular pixel spacing
        of 1 coordinate unit), set this parameter to the smallest
        value that keeps these areas from being eroded.
        The purpose of this is to prevent unnecessary computation.

    Returns
    -------
    concave_hull_traverse_delaunay : tuple
        Maximum and minimum erosion tolerance, information on
        edges considered for erosion (endpoint indices, edge length,
        critical value of erosion tolerance, index of third point in
        triangle considered for erosion), and a list of edges that
        play a direct role in determining the minimum erosion tolerance.

    Notes
    -----
    Edges in the triangulation are considered for erosion based
    on side length, and it is from side length that critical values
    of erosion tolerance are determined. In code, side length is
    referred to as "alpha", with global maximum and minimum lengths
    considered for erosion `alpha_max` and `alpha_min`. An edge
    that is considered for erosion has a particular local minimum
    erosion tolerance `local_mam` ("local max alpha min") which is
    the critical value at which an alpha cutoff value (see doc for
    `concave_hull_image`::`alpha_cutoff_mode`) less than `local_mam`
    value results in this edge being eroded.
    It is called local *max* alpha min because the local minimum
    erosion tolerance for an edge down one path (from a convex hull
    edge) may be less than the local minimum erosion tolerance for
    the same edge down a different path (from either the same or a
    different convex hull edge). All paths are traversed iteratively
    but in a recursive fashion to catalog the maximum of local
    minimum erosion tolerance values for each edge considered for
    erosion, along with the correct third point in the triangle
    that should be eroded if the edge is eroded.

    References
    ----------
    .. [1] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.spatial.Delaunay.html
    .. [2] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.spatial.Delaunay.convex_hull.html#scipy.spatial.Delaunay.convex_hull
    .. [3] https://docs.scipy.org/doc/scipy-0.14.0/reference/generated/scipy.spatial.Delaunay.vertex_neighbor_vertices.html#scipy.spatial.Delaunay.vertex_neighbor_vertices

    """
    indices, indptr = vertex_neighbor_vertices

    alpha_min = boundary_res
    alpha_max = boundary_res
    edge_info = {}
    revisit_edges = deque()
    amin_edges = set()

    for k1, k2 in convex_hull:
        next_edge = (k1, k2) if k1 < k2 else (k2, k1)
        p1, p2 = boundary_points[[k1, k2]]
        p1_p2 = p2 - p1
        next_alpha = np.sqrt(np.sum(np.square(p2 - p1)))
        alpha_max = max(alpha_max, next_alpha)
        if abs(p1_p2[0]) > boundary_res or abs(p1_p2[1]) > boundary_res:
            # Start traversing triangulation
            # from this convex hull edge.
            k3 = set(indptr[indices[k1]:indices[k1+1]]).intersection(
                 set(indptr[indices[k2]:indices[k2+1]])).pop()
            edge_info[next_edge] = [next_alpha, next_alpha, k3]
            revisit_edges.append((next_edge, k3))

            while len(revisit_edges) > 0:
                # Resume traversal from the "other" edge
                # in a traversed triangle.
                next_edge, revisit_k3 = revisit_edges.pop()
                k1, k2 = next_edge
                k3 = revisit_k3
                revisit_edge_info = edge_info[next_edge]
                local_mam = revisit_edge_info[1]
                p1, p2, p3 = boundary_points[[k1, k2, k3]]

                while True:
                    forward_edges = []
                    edge_1_3 = None
                    p1_p3 = p3 - p1
                    edge_2_3 = None
                    p2_p3 = p3 - p2
                    # Limit edges traversed, filtering by
                    # edge lengths longer than boundary_res.
                    if abs(p1_p3[0]) > boundary_res or abs(p1_p3[1]) > boundary_res:
                        edge_1_3 = (k1, k3) if k1 < k3 else (k3, k1)
                        forward_edges.append(edge_1_3)
                    if abs(p2_p3[0]) > boundary_res or abs(p2_p3[1]) > boundary_res:
                        edge_2_3 = (k2, k3) if k2 < k3 else (k3, k2)
                        forward_edges.append(edge_2_3)

                    next_edge = None
                    for fedge in forward_edges:
                        ka, kb = fedge
                        # Determine the third point in the forward triangle.
                        kc = set(indptr[indices[ka]:indices[ka+1]]).intersection(
                             set(indptr[indices[kb]:indices[kb+1]])).difference({k1, k2})
                        if not kc:
                            # We've arrived at a convex hull edge.
                            if fedge == edge_1_3:
                                edge_1_3 = None
                            else:
                                edge_2_3 = None
                            continue
                        kc = kc.pop()

                        if fedge not in edge_info:
                            # Catalog this edge.
                            fedge_alpha = np.sqrt(np.sum(np.square(p1_p3 if fedge == edge_1_3 else p2_p3)))
                            fedge_mam = min(local_mam, fedge_alpha)
                            edge_info[fedge] = [fedge_alpha, fedge_mam, kc]

                        else:
                            # Update max alpha min for this edge.
                            fedge_info = edge_info[fedge]
                            fedge_alpha, fedge_mam_old, _ = fedge_info
                            fedge_mam = min(local_mam, fedge_alpha)
                            if fedge_mam > fedge_mam_old:
                                # Update third point in this edge's triangle
                                # with that of the forward triangle.
                                fedge_info[1] = fedge_mam
                                fedge_info[2] = kc
                            else:
                                # Raise global alpha min to this edge's
                                # max alpha min value if it is lower,
                                # and halt traversal on this path.
                                if fedge_mam > alpha_min:
                                    alpha_min = fedge_mam
                                    amin_edges.add(fedge)
                                if fedge == edge_1_3:
                                    edge_1_3 = None
                                else:
                                    edge_2_3 = None
                                continue

                        if next_edge is None:
                            # Traverse forward on this edge.
                            next_edge = fedge
                            next_mam = fedge_mam
                            next_k3 = kc

                    if next_edge is not None:
                        # Continue forward traversal on the
                        # first of two possible forward edges.
                        # edge_1_3, if passed boundary_res check,
                        # takes priority over edge_2_3.
                        if edge_1_3 is not None:
                            if next_edge[0] == k1:
                                # p1 = p1
                                p2 = p3
                            else:
                                p2 = p1
                                p1 = p3

                            if edge_2_3 is not None:
                                # Save edge_2_3 along with the third
                                # point in its forward triangle,
                                # to be traversed once the current
                                # traversal reaches its end.
                                revisit_edges.append((edge_2_3, kc))
                        else:
                            if next_edge[0] == k2:
                                p1 = p2
                                p2 = p3
                            else:
                                p1 = p3
                                # p2 = p2

                        k1, k2 = next_edge
                        k3 = next_k3
                        p3 = boundary_points[next_k3]
                        local_mam = next_mam

                        if revisit_k3:
                            # The revisited edge was successfully
                            # traversed, so make sure the third point
                            # in its forward triangle is set accordingly.
                            revisit_edge_info[2] = revisit_k3
                            revisit_k3 = None
                    else:
                        break

    return alpha_min, alpha_max, edge_info, amin_edges


def concave_hull_image(image, concavity, fill=True, alpha_cutoff_mode='unique',
                       boundary_res=3, debug=False):
    """
    Compute the concave hull image of a binary image.
    The concave hull image is the convex hull image with edges
    of the hull eroded where the hull can have a tighter fit
    to the data without losing any coverage of data pixels
    (here, "data" refers to pixels with non-zero value).

    Parameters
    ----------
    image : ndarray, 2D
        Binary array from which to extract the concave hull image.
    concavity : 0 <= float <= 1
        How much to erode the edges of the convex hull.
        If 0, does not erode the edges of the convex hull,
        so what is returned is the convex hull image.
        If 1, erodes the edges of the convex hull with the
        smallest possible erosion tolerance ("alpha length")
        that keeps the concave hull from splitting into
        multiple polygons.
    fill : bool
        Whether or not to fill the concave hull in the returned image.
        If True, fill the concave hull.
        If False, let the concave hull have a 1-px-wide border.
    alpha_cutoff_mode : str; 'mean', 'median', or 'unique'
        The method used to determine the erosion threshold.
        If 'mean', `alpha_cut = (alpha_min + alpha_max) / 2`.
        If 'median', set `alpha_cut` to the median value from
        the set of all max alpha min values of edges.
        If 'unique', set `alpha_cut` to the median value from
        the set of all unique max alpha min (mam) values of edges.
        See docs for `concave_hull_traverse_delaunay` for
        details on the relationship between "alpha" values
        and erosion tolerance.
    boundary_res : positive int (3 appears safe for ~10k x ~10k pixel images)
        Minimum x or y *coordinate-wise* distance between two points
        in a triangle for their edge to be traversed, thereby allowing
        the triangle on the other side of that edge to be considered
        for erosion.
        If there are regions in the triangulation associated with
        a particular minimum point density whose boundaries should
        not be breached by erosion (such as regions of "good data"
        points taken from an image that have a regular pixel spacing
        of 1 coordinate unit), set this parameter to the smallest
        value that keeps these areas from being eroded.
        The purpose of this is to prevent unnecessary computation.
    debug : bool
        Whether or not to interrupt the run of this function
        with 3 plots displaying the Delaunay triangulation of
        the image as the function progresses through stages.

    Returns
    -------
    concave_hull_image : ndarray, 2D, same shape as `image`
        Binary image with pixels in concavve hull set to True.

    """
    if 0 <= concavity <= 1:
        pass
    else:
        raise InvalidArgumentError("`concavity` must be between 0 and 1, inclusive, "
                                   "but was {}".format(concavity))
    if alpha_cutoff_mode not in ('mean', 'median', 'unique'):
        raise UnsupportedMethodError("alpha_cutoff_mode='{}'".format(alpha_cutoff_mode))

    # Find data coverage boundaries.
    data_boundary = bwboundaries_array(image, connectivity=8, noholes=True, side='inner')
    boundary_points = np.argwhere(data_boundary)

    if debug:
        import matplotlib.pyplot as plt
    else:
        del data_boundary

    # Create the Delaunay triangulation.
    tri = scipy.spatial.Delaunay(boundary_points)

    if debug in (True, 1):
        print "[DEBUG] concave_hull_image (1): Initial triangulation plot"
        plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], tri.simplices.copy(), lw=1)
        plt.plot(boundary_points[:, 1], -boundary_points[:, 0], 'o', ms=1)
        plt.show()

    # Extract information from triangulation.
    hull_convex = tri.convex_hull
    vertex_neighbor_vertices = tri.vertex_neighbor_vertices
    indices, indptr = vertex_neighbor_vertices

    # Retrieve edge information for erosion from triangulation.
    alpha_min, alpha_max, edge_info, amin_edges = concave_hull_traverse_delaunay(
        boundary_points, hull_convex, vertex_neighbor_vertices, boundary_res
    )

    # Determine alpha cutoff value.
    alpha_cut = None
    if concavity == 0 or alpha_min == alpha_max:
        alpha_cut = np.inf
    elif alpha_cutoff_mode == 'mean':
        alpha_cut = (alpha_min + alpha_max) / 2
    elif alpha_cutoff_mode in ('median', 'unique'):
        mam_allowed = [einfo[1] for einfo in edge_info.values() if einfo[1] > alpha_min]
        if not mam_allowed:
            warn("Of {} total edges in edge_info, none have mam > alpha_min={}".format(len(edge_info), alpha_min))
            alpha_cut = np.inf
        else:
            if alpha_cutoff_mode == 'unique':
                mam_allowed = list(set(mam_allowed))
            mam_allowed.sort()
            alpha_cut = mam_allowed[-int(np.ceil(len(mam_allowed) * concavity))]
        del mam_allowed

    # Show triangulation traversal and allow modifying concavity parameter,
    # setting alpha_cut based on alpha_cutoff_mode, or modify alpha_cut itself.
    if debug in (True, 2):
        print "[DEBUG] concave_hull_image (2): Triangulation traversal"
        print "alpha_min = {}".format(alpha_min)
        print "alpha_max = {}".format(alpha_max)
        print "concavity = {}".format(concavity)
        print "alpha_cut = {}".format(alpha_cut)
        while True:
            erode_simplices = []
            erode_tris_mam = []
            amin_instances = {}
            for edge in edge_info:
                einfo = edge_info[edge]
                if einfo[1] >= alpha_cut:
                    erode_simplices.append([edge[0], edge[1], einfo[2]])
                    erode_tris_mam.append(einfo[1])
                if einfo[1] == alpha_min:
                    amin_tris = []
                    amin_instances[edge] = amin_tris
                    for k1 in edge:
                        amin_neighbors = indptr[indices[k1]:indices[k1+1]]
                        for k2 in amin_neighbors:
                            possible_k3 = set(indptr[indices[k1]:indices[k1+1]]).intersection(set(indptr[indices[k2]:indices[k2+1]]))
                            for k3 in possible_k3:
                                amin_tris.append([k1, k2, k3])
            plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], tri.simplices.copy(), lw=1)
            if erode_simplices:
                plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], erode_simplices, color='black', lw=1)
                plt.tripcolor(boundary_points[:, 1], -boundary_points[:, 0], erode_simplices, facecolors=np.array(erode_tris_mam), lw=1)
            for amin_edge in amin_edges:
                plt.plot(boundary_points[amin_edge, 1], -boundary_points[amin_edge, 0], 'r--', lw=1)
            for amin_edge in amin_instances:
                amin_tris = amin_instances[amin_edge]
                plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], amin_tris, color='red', lw=1)
            plt.plot(boundary_points[:, 1], -boundary_points[:, 0], 'o', ms=1)
            for hull_edge in hull_convex:
                plt.plot(boundary_points[hull_edge, 1], -boundary_points[hull_edge, 0], 'yo', lw=1.5)
            for amin_edge in amin_instances:
                plt.plot(boundary_points[amin_edge, 1], -boundary_points[amin_edge, 0], 'ro', lw=1.5)
            plt.show()
            user_input = raw_input("Modify params? (y/n): ")
            if user_input.lower() != "y":
                break
            validInput = False
            while not validInput:
                try:
                    user_input = raw_input("concavity = ")
                    if user_input == "":
                        break
                    else:
                        user_input_num = float(user_input)
                    if 0 <= user_input_num <= 1:
                        pass
                    else:
                        raise ValueError
                    concavity = user_input_num

                    alpha_cut = None
                    if concavity == 0 or alpha_min == alpha_max:
                        alpha_cut = np.inf
                    elif alpha_cutoff_mode == 'mean':
                        alpha_cut = (alpha_min + alpha_max) / 2
                    elif alpha_cutoff_mode in ('median', 'unique'):
                        mam_allowed = [einfo[1] for einfo in edge_info.values() if einfo[1] > alpha_min]
                        if not mam_allowed:
                            warn("Of {} total edges in edge_info, none have mam > alpha_min={}".format(len(edge_info), alpha_min))
                            alpha_cut = np.inf
                        else:
                            if alpha_cutoff_mode == 'unique':
                                mam_allowed = list(set(mam_allowed))
                            mam_allowed.sort()
                            alpha_cut = mam_allowed[-int(np.ceil(len(mam_allowed) * concavity))]
                        del mam_allowed

                    validInput = True
                    print "alpha_cut = {}".format(alpha_cut)
                except ValueError:
                    print "concavity must be an int or float between 0 and 1"
            while not validInput:
                try:
                    user_input = raw_input("alpha_cut = ")
                    if user_input == "":
                        break
                    else:
                        user_input_num = float(user_input)
                    alpha_cut = user_input_num
                    validInput = True
                except ValueError:
                    print "alpha_cut must be an int or float"

    # Gather eroded triangles and triangles containing edges
    # with length equal to alpha_min.
    erode_tris = []
    amin_instances = []
    for edge in edge_info:
        einfo = edge_info[edge]
        if einfo[1] >= alpha_cut:
            erode_tris.append(shapely.geometry.Polygon(boundary_points[[edge[0], edge[1], einfo[2]]]))
        if einfo[1] == alpha_min:
            amin_indices = []
            amin_instances.append(amin_indices)
            for k1 in edge:
                amin_neighbors = indptr[indices[k1]:indices[k1+1]]
                for k2 in amin_neighbors:
                    possible_k3 = set(indptr[indices[k1]:indices[k1+1]]).intersection(set(indptr[indices[k2]:indices[k2+1]]))
                    for k3 in possible_k3:
                        amin_indices.extend([k1, k2, k3])

    # Create convex hull (single) polygon, erosion region(s) (likely multi-)polygon,
    # and a polygon composed of edges that have an alpha equal to alpha_min.
    erode_poly = shapely.ops.unary_union(erode_tris)
    amin_poly = shapely.ops.unary_union(
        [shapely.geometry.MultiPoint(boundary_points[np.unique(indices)]).convex_hull for indices in amin_instances]
    )
    hull_convex_poly = shapely.geometry.MultiPoint(boundary_points[np.unique(hull_convex)]).convex_hull

    # Create concave hull (single) polygon.
    hull_concave_poly = hull_convex_poly.difference(erode_poly.difference(amin_poly))
    if type(hull_concave_poly) == shapely.geometry.polygon.Polygon:
        hull_concave_poly = [hull_concave_poly]
    else:
        warn("Concave hull is broken into multiple polygons; try increasing data_boundary_res")

    del erode_poly, amin_poly, hull_convex_poly

    # Draw concave hull image.
    mask = np.zeros(image.shape, dtype=np.bool)
    for poly in hull_concave_poly:
        cchull_r, cchull_c = poly.exterior.coords.xy
        cchull_r = np.array(cchull_r)
        cchull_c = np.array(cchull_c)

        if debug in (True, 3):
            print "[DEBUG] concave_hull_image (3): Concave hull boundary points"
            plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], tri.simplices.copy(), lw=1)
            if erode_simplices:
                plt.triplot(boundary_points[:, 1], -boundary_points[:, 0], erode_simplices, color='red', lw=1)
            plt.plot(boundary_points[:, 1], -boundary_points[:, 0], 'o', ms=1)
            plt.plot(cchull_c, -cchull_r, 'ro', ms=3)
            i = 0
            for xy in np.column_stack((cchull_c, -cchull_r)):
                i += 1
                plt.annotate(s=str(i), xy=xy)
            plt.show()

        draw_r, draw_c = polygon_perimeter(cchull_r, cchull_c)
        mask[draw_r, draw_c] = 1

    if fill:
        mask = sp_ndimage.morphology.binary_fill_holes(mask)

    # TODO: Remove the following before sharing algorithm.
    if debug in (True, 4):
        debug_mask = np.zeros(image.shape, dtype=np.int8)
        debug_mask[mask] = 1
        debug_mask[data_boundary] += 2
        test.saveImage(debug_mask, 'debug_concave_hull_image')

    return mask


def getWindow(array, window_shape, x_y_tup, one_based_index=True):
    # TODO: Write docstring.
    # FIXME: Needs error checking on array bounds.
    window_ysize, window_xsize = window_shape
    colNum, rowNum = x_y_tup
    if one_based_index:
        rowNum -= 1
        colNum -= 1
    return array[int(rowNum-np.floor((window_ysize-1)/2)):int(rowNum+np.ceil((window_ysize-1)/2)+1),
                 int(colNum-np.floor((window_xsize-1)/2)):int(colNum+np.ceil((window_xsize-1)/2)+1)]



################################
# Data Boundary Polygonization #
################################


def getFPvertices(array, X, Y,
                  tolerance_start=100, nodataVal=np.nan, method='convhull'):
    """
    Polygonizes the generalized (hull) boundary of all data clusters in a
    NumPy 2D array with supplied grid coordinates [X, Y] (ranges in 1D array form)
    and simplifies this boundary until it contains 80 or fewer vertices.
    These 'footprint' vertices are returned as a tuple containing lists
    of all x-coordinates and all y-coordinates of these (ordered) points.
    """
    if nodataVal != np.nan:
        array_data = (array != nodataVal)
    else:
        array_data = ~np.isnan(array)

    # Get the data boundary ring.
    if method == 'convhull':
        # Fill interior nodata holes.
        array_filled = sp_ndimage.morphology.binary_fill_holes(array_data)
        try:
            ring = getFPring_nonzero(array_filled, X, Y)
        except MemoryError:
            print "MemoryError on call to getFPring_convhull in raster_array_tools:"
            print_exc()
            print "-> Defaulting to getFPring_nonzero"
            del array_filled
            ring = getFPring_nonzero(array_data, X, Y)

    elif method == 'nonzero':
        ring = getFPring_nonzero(array_data, X, Y)

    else:
        raise UnsupportedMethodError("method='{}'".format(method))

    del array_data
    if 'array_filled' in vars():
        del array_filled

    numVertices = ring.GetPointCount()
    if numVertices > 80:
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)

        # Simplify the geometry until it has 80 or fewer points.
        toler = tolerance_start
        while numVertices > 80:
            poly = poly.SimplifyPreserveTopology(toler)
            ring = poly.GetGeometryRef(0)
            numVertices = ring.GetPointCount()
            if numVertices > 400:
                toler += 1000
            elif numVertices > 200:
                toler += 500
            elif numVertices > 100:
                toler += 300
            else:
                toler += 200

    boundary_points = ring.GetPoints()

    points_xlist = map(lambda point_tup: point_tup[0], boundary_points)
    points_ylist = map(lambda point_tup: point_tup[1], boundary_points)

    return points_xlist, points_ylist


def getFPring_convhull(array_filled, X, Y):
    """
    Traces the boundary of a (large) pre-hole-filled data mass in array_filled
    using a convex hull function.
    Returns an OGRGeometry object in ogr.wkbLinearRing format representing
    footprint vertices of the data mass, using [X, Y] grid coordinates.
    """
    # Derive data cluster boundaries (in array representation).
    data_boundary = (array_filled != sp_ndimage.binary_erosion(array_filled))

    boundary_points = np.argwhere(data_boundary)
    del data_boundary

    # Convex hull method.
    convex_hull = scipy.spatial.ConvexHull(boundary_points)
    hull_points = boundary_points[convex_hull.vertices]
    del convex_hull

    # Assemble the geometry.
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for p in hull_points:
        ring.AddPoint_2D(X[p[1]], Y[p[0]])  # Make points (x-coord, y-coord)
    # Close the ring.
    ring.AddPoint_2D(X[hull_points[0][1]],
                     Y[hull_points[0][0]])

    return ring


def getFPring_nonzero(array_data, X, Y):
    """
    Traces a simplified boundary of a (large) pre-hole-filled data mass
    in array_filled by making one scan across the columns of the array
    and recording the top and bottom data points found in each column.
    Returns an OGRGeometry object in ogr.wkbLinearRing format representing
    footprint vertices of the data mass, using [X, Y] grid coordinates.
    """
    # Scan left to right across the columns in the binary data array,
    # building top and bottom routes that are simplified because they
    # cannot represent the exact shape of some 'eaten-out' edges.
    top_route = []
    bottom_route = []
    for colNum in range(array_data.shape[1]):
        rowNum_data = np.nonzero(array_data[:, colNum])[0]
        if rowNum_data.size > 0:
            top_route.append((rowNum_data[0], colNum))
            bottom_route.append((rowNum_data[-1], colNum))

    # Prepare the two routes (check endpoints) for connection.
    bottom_route.reverse()
    if top_route[-1] == bottom_route[0]:
        del top_route[-1]
    if bottom_route[-1] != top_route[0]:
        bottom_route.append(top_route[0])

    # Assemble the geometry.
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for p in top_route:
        ring.AddPoint_2D(X[p[1]], Y[p[0]])  # Make points (x-coord, y-coord)
    for p in bottom_route:
        ring.AddPoint_2D(X[p[1]], Y[p[0]])  # Make points (x-coord, y-coord)

    return ring


def getDataBoundariesPoly(array, X, Y, nodataVal=np.nan, coverage='all',
                          erode=False, BBS=True):
    """
    Polygonizes the boundaries of all data clusters in a NumPy 2D array,
    using supplied [X, Y] grid coordinates (ranges in 1D array form) for vertices.
    Returns an OGRGeometry object in ogr.wkbPolygon format.
    If coverage='outer', interior nodata holes are filled before tracing.
    If erode=True, data spurs are eliminated before tracing.
    If BBS=True, Bi-directional Boundary Skewing preprocessing is done.
    --Utilizes a fast boundary tracing method: outline.c
    """
    if nodataVal != np.nan:
        data_array = (array != nodataVal)
    else:
        data_array = ~np.isnan(array)

    if BBS:
        # Pad data array with zeros and extend grid coordinates arrays
        # in preparation for Bi-directional Boundary Skewing.
        dx = X[1]-X[0]
        dy = Y[1]-Y[0]
        data_array = np.pad(data_array, 2, 'constant')
        X_ext = np.concatenate((np.array([X[0]-2*dx, X[0]-dx]), X, np.array([X[-1]+dx, X[-1]+2*dx])))
        Y_ext = np.concatenate((np.array([Y[0]-2*dy, Y[0]-dy]), Y, np.array([Y[-1]+dy, Y[-1]+2*dy])))
    else:
        X_ext, Y_ext = X, Y

    if coverage == 'outer':
        # Fill interior nodata holes.
        data_array = sp_ndimage.morphology.binary_fill_holes(data_array)

    if erode:
        # Erode data regions to eliminate *any* data spurs (possible 1 or 2 pixel-
        # width fingers that stick out from data clusters in the original array).
        # This should make the tracing of data boundaries more efficient since
        # the rings that are traced will be more substantial.
        data_array = sp_ndimage.binary_erosion(data_array, structure=np.ones((3, 3)))
        if ~np.any(data_array):
            # No data clusters large enough to have a traceable boundary exist.
            return None
        # Reverse the erosion process to retrieve a data array that does not
        # contain data spurs, yet remains true to data coverage.
        data_array = sp_ndimage.binary_dilation(data_array, structure=np.ones((3, 3)))

    if BBS:
        # Bi-directional Boundary Skewing
        # To represent data coverage fairly for most data pixels in the raster image,
        # the right and bottom edges of all data boundaries must grow by one pixel so
        # that their full extents may be recorded upon grid coordinate lookup of data
        # boundary nodes after each boundary ring is traced.
        print "Performing Bi-directional Boundary Skewing"
        outer_boundary = (
            sp_ndimage.binary_dilation(data_array, structure=np.ones((3, 3))) != data_array
        )
        outer_boundary_nodes = [(row[0], row[1]) for row in np.argwhere(outer_boundary)]
        # In skew_check, 0 is the location of an outer boundary node to be checked, 'n'.
        # If there is a 1 in the data array at any of the three corresponding neighbor
        # locations, n will be set in the data array for data boundary tracing.
        skew_check = np.array([[1,1],
                               [1,0]])
        skew_additions = np.zeros_like(data_array)
        for n in outer_boundary_nodes:
            window = data_array[n[0]-1:n[0]+1, n[1]-1:n[1]+1]
            if np.any(skew_check & window):
                skew_additions[n] = 1
        data_array = (data_array | skew_additions)

    # Find data coverage boundaries.
    data_boundary = (data_array != sp_ndimage.binary_erosion(data_array))

    # Create polygon.
    poly = ogr.Geometry(ogr.wkbPolygon)
    ring_count = 0
    for colNum in range(data_boundary.shape[1]):
        rowNum_dataB = np.where(data_boundary[:, colNum])[0]
        while rowNum_dataB.size > 0:
            rowNum = rowNum_dataB[0]
            home_node = (rowNum, colNum)

            # Trace the data cluster.
            print "Tracing ring from home node {}".format(home_node)
            ring_route = outline(data_boundary, 1, start=home_node)

            # Create ring geometry.
            data_ring = ogr.Geometry(ogr.wkbLinearRing)
            for p in ring_route:
                data_ring.AddPoint_2D(X_ext[p[1]], Y_ext[p[0]])  # Make points (x-coord, y-coord)
                data_boundary[p] = 0    # Fill in ring in data boundary array
                                        # to mark that this ring is captured.
            # # (Alternative method of filling in ring.)
            # ring_route = np.array(ring_route).T  ## DOES preserve point order.
            # data_boundary[ring_route[0], ring_route[1]] = 0

            # Add ring to the polygon.
            poly.AddGeometry(data_ring)
            ring_count += 1

            # Search for more rings that may intersect this column in the data boundary array.
            rowNum_dataB = np.where(data_boundary[:, colNum])[0]

    print "Found {} rings!".format(ring_count)
    return poly


def outline(array, every, start=None, pass_start=False, complete_ring=True):
    """
    Taking an (binary) array as input, finds the first set node in the array
    (by scanning down each column as it scans left to right across the array)
    [may instead be specified by giving the argument start=(row, col)]
    and traces the inner boundary of the structure (of set nodes) it is
    connected to, winding up back at this starting node.
    The path taken is returned as an ordered list of nodes in (row, col) form
    where every "every"-th node is reported.
    If pass_start=False, the path is complete when the first node is revisited.
    If pass_start=True, the path is complete when the first AND second nodes are revisited.
    If complete_ring=True, the first and last nodes in the returned route will match.
    --Utilizes a fast boundary tracing method: (outline.c / outline_every1.c)
    --Does not adhere to either connectivity 1 or 2 rule in data boundary path-finding.
    Both this function and outline.c have been modified from their original forms, found at:
    http://stackoverflow.com/questions/14110904/numpy-binary-raster-image-to-polygon-transformation
    """
    if type(array) != np.ndarray:
        raise InvalidArgumentError("`array` must be of type numpy.ndarray")
    if type(every) != int or every < 1:
        raise InvalidArgumentError("`every` must be a positive integer")

    if len(array) == 0:
        return np.array([])

    # Set up arguments to (outline.c / outline_every1.c)
    if start is not None:
        rows, cols = array.shape
        starty, startx = start
        if starty < 0 or starty >= rows or startx < 0 or startx >= cols:
            raise InvalidArgumentError("Invalid `start` node: {}".format(start))
        starty += 1
        startx += 1
    else:
        starty, startx = -1, -1
    pass_start = int(pass_start)
    data = np.pad(array, 1, 'constant')
    rows, cols = data.shape

    if every != 1:
        padded_route = scipy.weave.inline(
            _outline, ['data', 'rows', 'cols', 'every', 'starty', 'startx', 'pass_start'],
            type_converters=scipy.weave.converters.blitz
        )
        if complete_ring and (len(padded_route) > 0) and (padded_route[0] != padded_route[-1]):
            padded_route.append(padded_route[0])
    else:
        padded_route = scipy.weave.inline(
            _outline_every1, ['data', 'rows', 'cols', 'starty', 'startx', 'pass_start'],
            type_converters=scipy.weave.converters.blitz
        )

    fixed_route = [(row[0], row[1]) for row in (np.array(padded_route) - 1)]

    return fixed_route


def connectEdges(edge_collection, allow_modify_deque_input=True):
    # TODO: Test function.
    """
    Takes a collection of edges, each edge being an ordered collection of vertex numbers,
    and recursively connects them by linking edges with endpoints that have matching vertex numbers.
    A reduced list of edges (each edge as a deque) is returned. This list will contain multiple edge
    components if the input edges cannot be connected to form a single unbroken edge.
    If allow_modify_deque_input=True, an input edge_list containing deque edge components will be
    modified.
    """
    edges_input = None
    if type(edge_collection[0]) == deque:
        edges_input = edge_collection if allow_modify_deque_input else copy.deepcopy(edge_collection)
    else:
        edges_input = [deque(e) for e in edge_collection]

    while True:
        edges_output = []
        for edge_in in edges_input:
            edge_in_end_l, edge_in_end_r = edge_in[0], edge_in[-1]
            connected = False
            for edge_out in edges_output:
                if edge_out[0] == edge_in_end_l:
                    edge_out.popleft()
                    edge_out.extendleft(edge_in)
                    edge_in_added = True
                    break
                if edge_out[0] == edge_in_end_r:
                    edge_out.popleft()
                    edge_in.reverse()
                    edge_out.extendleft(edge_in)
                    edge_in_added = True
                    break
                if edge_out[-1] == edge_in_end_l:
                    edge_out.pop()
                    edge_out.extend(edge_in)
                    edge_in_added = True
                    break
                if edge_out[-1] == edge_in_end_r:
                    edge_out.pop()
                    edge_in.reverse()
                    edge_out.extend(edge_in)
                    edge_in_added = True
                    break
            if not connected:
                edges_output.append(edge_in)
        if len(edges_output) == len(edges_input):
            return edges_output
        else:
            edges_input = edges_output
