import numpy as np
import math
import os
import h5py
import dask.array as da
import time as ttime
from dask.distributed import Client, wait
from progress.bar import Bar
from skbeam.core.fitting.background import snip_method
from .fitting import fit_spectrum

import logging
logger = logging.getLogger()


class TerminalProgressBar:
    """
    Custom class that displays the progress bar in the terminal. Progress
    is displayed in %. Unfortunately it works only in the terminal (or emulated
    terminal) and nothing will be printed in stderr if TTY is disabled.

    Examples
    --------
    .. code-block:: python

        title = "Monitor progress"
        pbar = TerminalProgressBar(title)
        pbar.start()
        for n in range(10):
           pbar(n * 10)  # Go from 0 to 90%
        pbar.finish()  # This should set it to 100%
    """

    def __init__(self, title):
        self.title = title

    def start(self):
        self.bar = Bar(self.title, max=100, suffix='%(percent)d%%')

    def __call__(self, percent_completed):
        while self.bar.index < percent_completed:
            self.bar.next()

    def finish(self):
        while self.bar.index < 100.0:
            self.bar.next()
        self.bar.finish()


def wait_and_display_progress(fut, progress_bar=None):
    """
    Wait for the future to complete and display the progress bar.
    This method may be used to drive any custom progress bar, which
    displays progress in percent from 0 to 100.

    Parameters
    ----------
    fut: dask future
        future object for the batch of tasks submitted to the distributed
        client.
    progress_bar: callable or None
        callable function or callable object with methods `start()`,
        `__call__(float)` and `finish()`. The methods `start()` and
        `finish()` are optional. For example, this could be a reference
        to an instance of the object `TerminalProgressBar`

    Examples
    --------

    .. code-block::

        client = Client()
        data = da.random.random(size=(100, 100), chunks=(10, 10))
        sm_fut = da.sum(data, axis=0).persist(scheduler=client)

        # Call the progress monitor
        wait_and_display_progress(sm_fut, TerminalProgressBar("Monitoring progress: "))

        sm = sm_fut.compute(scheduler=client)
        client.close()
    """

    # If there is no progress bar, then just return without waiting for the future
    if progress_bar is None:
        return

    if hasattr(progress_bar, "start"):
        progress_bar.start()

    while True:
        done, not_done = wait(fut, return_when='FIRST_COMPLETED')
        n_completed, n_pending = len(done), len(not_done)
        n_total = n_completed + n_pending
        percent_completed = n_completed / n_total * 100.0 if n_total > 0 else 100.0

        # It is guaranteed that 'progress_bar' is called for 100% completion
        progress_bar(percent_completed)

        if not n_pending:
            break
        ttime.sleep(0.5)

    if hasattr(progress_bar, "finish"):
        progress_bar.finish()


class RawHDF5Dataset():
    """
    Instead of actual data we may store the HDF5 file name and dataset name within the
    HDF5 file. When access is needed, then data may be loaded from file. Typically
    this will be used for keeping reference to large dataset that will be loaded in
    'lazy' way during processing.

    Parameters
    ----------
    abs_path: str
        absolute path to the HDF5 file
    dset_name: str
        name of the dataset in the HDF5 file
    shape: tuple, optional
        if shape is provided (not None), then the object will have additional
        attribute 'shape'. Keeping shape information is sometimes convenient.
    """
    def __init__(self, _abs_path, _dset_name, shape=None):
        self.abs_path = os.path.abspath(os.path.expanduser(_abs_path))
        self.dset_name = _dset_name
        if shape is not None:
            self.shape = shape


def _compute_optimal_chunk_size(chunk_pixels, data_chunksize, data_shape, n_chunks_min=4):
    """
    Compute the best chunk size for the 'data' array based on existing size and
    chunk size of the `data` array and the desired number of pixels in the chunk.
    The new chunk size will contain whole number of original chunks of the `data`
    array and at least `chunk_pixels` pixels. Image pixels are determined by axes
    0 and 1. The remaining axes (typically axis 2) are not considered during this
    rechunking procedure.

    Parameters
    ----------
    chunk_pixels: int
        The desired number of pixels in the new chunks
    data_chunksize: tuple(int)
        (chunk_y, chunk_x) - original chunk size of the data array
    data_shape: tuple(int)
        (ny, nx) - the shape of the data array
    n_chunks_min: int
        The minimum number of chunks.

    Returns
    -------
    (chunk_y, chunk_x): tuple(int)
        The size of the new chunks along axes 0 and 1
    """

    if not isinstance(data_chunksize, tuple) or len(data_chunksize) != 2:
        raise ValueError(f"Unsupported value of parameter 'data_chunksize': {data_chunksize}")
    if not isinstance(data_shape, tuple) or len(data_shape) != 2:
        raise ValueError(f"Unsupported value of parameter 'data_shape': {data_shape}")

    # Compute chunk size based on the desired number of pixels and chunks in the 'data' array
    dset_chunks, dset_shape = data_chunksize, data_shape
    # Desired number of pixels in the chunk
    n_pixels_total = dset_shape[0] * dset_shape[1]
    if n_pixels_total > n_chunks_min:
        # We want to have at least 1 pixel in the chunk
        chunk_pixels = min(chunk_pixels, n_pixels_total // n_chunks_min)
    # Try to make chunks square (math.sqrt)
    chunk_x = int(math.ceil(math.sqrt(chunk_pixels) / dset_chunks[1]) * dset_chunks[1])
    chunk_x = min(chunk_x, dset_shape[1])  # The array may have few columns
    chunk_y = int(math.ceil(chunk_pixels / chunk_x / dset_chunks[0]) * dset_chunks[0])
    if chunk_y > dset_shape[0]:
        # Now explore the case if the array has small number of rows. We may want to stretch
        #   the chunk horizontally
        chunk_y = dset_shape[0]
        chunk_x = int(math.ceil(chunk_pixels / chunk_y / dset_chunks[1]) * dset_chunks[1])
        chunk_x = min(chunk_x, dset_shape[1])

    return chunk_y, chunk_x


def _chunk_numpy_array(data, chunk_size):
    """
    Convert a numpy array into Dask array with chunks of given size. The function
    splits the array into chunks along axes 0 and 1. If the array has more than 2 dimensions,
    then the remaining dimensions are not chunked. Note, that
    `dask_array = da.array(data, chunks=...)` will set the chunk size, but not split the
    data into chunks, therefore the array can not be loaded block by block by workers
    controlled by a distributed scheduler.

    Parameters
    ----------
    data: ndarray(float), 2 or more dimensions
        XRF map of the shape `(ny, nx, ne)`, where `ny` and `nx` represent the image size
        and `ne` is the number of points in spectra
    chunk_size: tuple(int, int) or list(int, int)
         Chunk size for axis 0 and 1: `(chunk_y, chunk_x`). The function will accept
         chunk size values that are larger then the respective `data` array dimensions.

    Returns
    -------
    data_dask: dask.array
        Dask array with the given chunk size
    """

    chunk_y, chunk_x = chunk_size
    ny, nx = data.shape[0:2]
    chunk_y, chunk_x = min(chunk_y, ny), min(chunk_x, nx)

    def _get_slice(n1, n2):
        data_slice = data[slice(n1 * chunk_y, min(n1 * chunk_y + chunk_y, ny)),
                          slice(n2 * chunk_x, min(n2 * chunk_x + chunk_x, nx))]
        # Wrap the slice into a list wiht appropriate dimensions
        for _ in range(2, data.ndim):
            data_slice = [data_slice]
        return data_slice

    # Chunk the numpy array and assemble it as a dask array
    data_dask = da.block([
        [
            _get_slice(_1, _2)
            for _2 in range(int(math.ceil(nx / chunk_x)))
        ]
        for _1 in range(int(math.ceil(ny / chunk_y)))
    ])

    return data_dask


def _array_numpy_to_dask(data, chunk_pixels, n_chunks_min=4):
    """
    Convert an array (e.g. XRF map) from numpy array to chunked Dask array. Select chunk
    size based on the desired number of pixels `chunk_pixels`. The array is considered
    as an image with pixels along axes 0 and 1. The array is chunked only along axes 0 and 1.

    Parameters
    ----------
    data: ndarray(float), 3D
        Numpy array of the shape `(ny, nx, ...)` with at least 2 dimensions. If `data` is
        an image, then `ny` and `nx` represent the image dimensions.
    chunk_pixels: int
        Desired number of pixels in a chunk. The actual number of pixels may differ from
        the desired number to accommodate minimum requirements on the number of chunks or
        limited size of the dataset.
    n_chunks_min: int
        minimum number of chunks, which should be selected based on the minimum number of
        workers that should be used to process the map. Each chunk will contain at least
        one pixel: if there is not enough pixels, then the number of chunks will be reduced.

    Results
    -------
    Dask array of the same shape as `data` with chunks selected based on the desired number
    of pixels `chunk_pixels`.
    """

    if not isinstance(data, np.ndarray) or (data.ndim < 2):
        raise ValueError(f"Parameter 'data' must numpy array with at least 2 dimensions: "
                         f"type(data)={type(data)}")

    ny, nx = data.shape[0:2]
    # Since numpy array is not chunked by default, set the original chunk size to (1,1)
    #   because here we are performing 'original' chunking
    chunk_y, chunk_x = _compute_optimal_chunk_size(chunk_pixels=chunk_pixels,
                                                   data_chunksize=(1, 1),
                                                   data_shape=(ny, nx),
                                                   n_chunks_min=n_chunks_min)

    return _chunk_numpy_array(data, (chunk_y, chunk_x))


def _prepare_xrf_map(data, chunk_pixels=5000, n_chunks_min=4):

    """
    Convert XRF map from it's initial representation to properly chunked Dask array.

    Parameters
    ----------
    data: da.core.Array, np.ndarray or RawHDF5Dataset (this is a custom type)
        Raw XRF map represented as Dask array, numpy array or reference to a dataset in
        HDF5 file. The XRF map must have dimensions `(ny, nx, ne)`, where `ny` and `nx`
        define image size and `ne` is the number of spectrum points
    chunk_pixels: int
        The number of pixels in a single chunk. The XRF map will be rechunked so that
        each block contains approximately `chunk_pixels` pixels and contain all `ne`
        spectrum points for each pixel.
    n_chunks_min: int
        Minimum number of chunks. The algorithm will try to split the map into the number
        of chunks equal or greater than `n_chunks_min`.

    Returns
    -------
    data: da.core.Array
        XRF map represented as Dask array with proper chunk size. The XRF map may be loaded
        block by block when processing using `dask.array.map_blocks` and `dask.array.blockwise`
        functions with Dask multiprocessing scheduler.
    file_obj: h5py.File object
        File object that points to HDF5 file. `None` if input parameter `data` is Dask or
        numpy array. Note, that `file_obj` must be kept alive until processing is completed.
        Closing the file will invalidate references to the dataset in the respective
        Dask array.

    Raises
    ------
    TypeError if input parameter `data` is not one of supported types.
    """

    file_obj = None  # It will remain None, unless 'data' is 'RawHDF5Dataset'

    if isinstance(data, da.core.Array):
        chunk_size = _compute_optimal_chunk_size(chunk_pixels=chunk_pixels,
                                                 data_chunksize=data.chunksize[0:2],
                                                 data_shape=data.shape[0:2],
                                                 n_chunks_min=n_chunks_min)
        data = data.rechunk(chunks=(*chunk_size, data.shape[2]))
    elif isinstance(data, np.ndarray):
        data = _array_numpy_to_dask(data, chunk_pixels=chunk_pixels, n_chunks_min=n_chunks_min)
    elif isinstance(data, RawHDF5Dataset):
        fpath, dset_name = data.abs_path, data.dset_name

        # Note, that the file needs to remain open until the processing is complete !!!
        file_obj = h5py.File(fpath, "r")
        dset = file_obj[dset_name]

        if dset.ndim != 3:
            raise TypeError(f"Dataset '{dset_name}' in file '{fpath}' has {dset.ndim} dimensions: "
                            f"3D dataset is expected")
        ny, nx, ne = dset.shape
        chunk_size = _compute_optimal_chunk_size(chunk_pixels=chunk_pixels,
                                                 data_chunksize=dset.chunks[0:2],
                                                 data_shape=(ny, nx),
                                                 n_chunks_min=n_chunks_min)
        data = da.from_array(dset, chunks=(*chunk_size, ne))
    else:
        raise TypeError(f"Type of parameter 'data' is not supported: type(data)={type(data)}")

    return data, file_obj


def _prepare_xrf_mask(data, mask=None, selection=None):
    """
    Create mask for processing XRF maps based on the provided mask, selection
    and XRF dataset. If only `mask` is provided, then it is passed to the output.
    If only `selection` is provided, then the new mask is generated based on selected pixels.
    If both `mask` and `selection` are provided, then all pixels in the mask that fall outside
    the selected area are disabled. Input `mask` is a numpy array. Output `mask` is a Dask
    array with chunk size matching `data`.

    Parameters
    ----------
    data: da.core.Array
        dask array representing XRF dataset with dimensions (ny, nx, ne) and chunksize
        (chunk_y, chunk_x)
    mask: ndarray or None
        mask represented as numpy array with dimensions (ny, nx)
    selection: tuple or list or None
        selected area represented as (y0, x0, ny_sel, nx_sel)

    Returns
    -------
    mask: da.core.Array
        mask represented as Dask array of size (ny, nx) and chunk size (chunk_y, chunk_x)

    Raises
    ------
    TypeError if type of some input parameters is incorrect

    """

    if not isinstance(data, da.core.Array):
        raise TypeError(f"Parameter 'data' must be a Dask array: type(data) = {type(data)}")
    if data.ndim < 2:
        raise TypeError(f"Parameter 'data' must have at least 2 dimensions: data.ndim = {data.ndim}")
    if mask is not None:
        if mask.shape != data.shape[0:2]:
            raise TypeError(f"Dimensions 0 and 1 of parameters 'data' and 'mask' do not match: "
                            f"data.shape={data.shape} mask.shape={mask.shape}")
    if selection is not None:
        if len(selection) != 4:
            raise TypeError(f"Parameter 'selection' must be iterable with 4 elements: "
                            f"selection = {selection}")

    if selection is not None:
        y0, x0, ny, nx = selection
        mask_sel = np.zeros(shape=data.shape[0:2])
        mask_sel[y0: y0 + ny, x0: x0 + nx] = 1

        if mask is None:
            mask = mask_sel
        else:
            mask = mask_sel * mask  # We intentionally create the copy of 'mask'

    if mask is not None:
        mask = (mask > 0).astype(dtype=int)
        chunk_y, chunk_x = data.chunksize[0:2]
        mask = _chunk_numpy_array(mask, (chunk_y, chunk_x))

    return mask


def compute_total_spectrum(data, *, selection=None, mask=None,
                           chunk_pixels=5000, n_chunks_min=4,
                           progress_bar=None, client=None):
    """
    Parameters
    ----------
    data: da.core.Array, np.ndarray or RawHDF5Dataset (this is a custom type)
        Raw XRF map represented as Dask array, numpy array or reference to a dataset in
        HDF5 file. The XRF map must have dimensions `(ny, nx, ne)`, where `ny` and `nx`
        define image size and `ne` is the number of spectrum points
    selection: tuple or list or None
        selected area represented as (y0, x0, ny_sel, nx_sel)
    mask: ndarray or None
        mask represented as numpy array with dimensions (ny, nx)
    chunk_pixels: int
        The number of pixels in a single chunk. The XRF map will be rechunked so that
        each block contains approximately `chunk_pixels` pixels and contain all `ne`
        spectrum points for each pixel.
    n_chunks_min: int
        Minimum number of chunks. The algorithm will try to split the map into the number
        of chunks equal or greater than `n_chunks_min`.
    progress_bar: callable or None
        reference to the callable object that implements progress bar. The example of
        such a class for progress bar object is `TerminalProgressBar`.
    client: dask.distributed.Client or None
        Dask client. If None, then local client will be created

    Returns
    -------
    result: ndarray
        Spectrum averaged over the XRF dataset taking into account mask and selectied area.
    """

    if not isinstance(mask, np.ndarray) and (mask is not None):
        raise TypeError(f"Parameter 'mask' must be a numpy array or None: type(mask) = {type(mask)}")

    data, file_obj = _prepare_xrf_map(data, chunk_pixels=chunk_pixels, n_chunks_min=n_chunks_min)
    mask = _prepare_xrf_mask(data, mask=mask, selection=selection)

    if client is None:
        client = Client(processes=True, silence_logs=logging.ERROR)
        client_is_local = True
    else:
        client_is_local = False

    n_workers = len(client.scheduler_info()["workers"])
    logger.info(f"Dask distributed client: {n_workers} workers")

    if mask is None:
        result_fut = da.sum(da.sum(data, axis=0), axis=0).persist(scheduler=client)
    else:
        def _masked_sum(data, mask):
            mask = np.broadcast_to(np.expand_dims(mask, axis=2), data.shape)
            sm = np.sum(np.sum(data * mask, axis=0), axis=0)
            return np.array([[sm]])
        result_fut = da.blockwise(_masked_sum, 'ijk', data, "ijk", mask, "ij", dtype="float")

    # Call the progress monitor
    wait_and_display_progress(result_fut, progress_bar)

    result = result_fut.compute(scheduler=client)

    if client_is_local:
        client.close()

    if mask is not None:
        # The sum computed for each block still needs to be assembled,
        #   but 'result' is much smaller array than 'data'
        result = np.sum(np.sum(result, axis=0), axis=0)
    return result


def _fit_xrf_block(data, data_sel_indices,
                   matv, snip_param, use_snip):
    """
    Spectrum fitting for a block of XRF dataset. The function is intended to be
    called using `map_blocks` function for parallel processing using Dask distributed
    package.

    Parameters
    ----------
    data : ndarray
        block of an XRF dataset. Shape=(ny, nx, ne).
    data_sel_indices: tuple
        tuple `(n_start, n_end)` which defines the indices along axis 2 of `data` array
        that are used for fitting. Note that `ne` (in `data`) and `ne_model` (in `matv`)
        are not equal. But `n_end - n_start` MUST be equal to `ne_model`! Indexes
        `n_start .. n_end - 1` will be selected from each pixel.
    matv: ndarray
        Matrix of spectra of the selected elements (emission lines). Shape=(ne_model, n_lines)
    snip_param: dict
        Dictionary of parameters forwarded to 'snip' method for background removal.
        Keys: `e_offset`, `e_linear`, `e_quadratic` (parameters of the energy axis approximation),
        `b_width` (width of the window that defines resolution of the snip algorithm).
    use_snip: bool, optional
        enable/disable background removal using snip algorithm

    Returns
    -------
    data_out: ndarray
        array with fitting results. Shape: `(ny, nx, ne_model + 4)`. For each pixel
        the output data contains: `ne_model` values that represent area under the emission
        line spectra; background area (only in the selected energy range), error (R-factor),
        total count in the selected energy range, total count of the full experimental spectrum.
    """

    data_out = np.zeros(shape=(data.shape[0], data.shape[1], matv.shape[1] + 4))

    for ny in range(data.shape[0]):
        for nx in range(data.shape[1]):

            # Full spectrum (all points)
            spec = data[ny, nx, :]
            spec_sel = spec[data_sel_indices[0]: data_sel_indices[1]]

            if use_snip:

                bg = snip_method(spec_sel,
                                 snip_param['e_offset'],
                                 snip_param['e_linear'],
                                 snip_param['e_quadratic'],
                                 width=snip_param['b_width'])
                y = spec_sel - bg
                # Force spectrum to be always positive for better performance of nnls
                y = np.clip(y, a_min=0, a_max=None)

                bg_sum = np.sum(bg)

            else:
                y = spec_sel
                bg_sum = 0

            weights, rfactor, _ = fit_spectrum(y, matv, method="nnls")

            # Total number of counts in the selected region
            total_cnt = np.sum(spec)
            sel_cnt = np.sum(spec_sel)

            result = np.concatenate((weights, np.array([bg_sum, rfactor,
                                                        sel_cnt, total_cnt])))

            data_out[ny, nx, :] = result

    return data_out


def fit_xrf_map(data, data_sel_indices, matv, snip_param, use_snip=True,
                chunk_pixels=5000, n_chunks_min=4, progress_bar=None, client=None):
    """
    Fit XRF map.

    Parameters
    ----------
    data: da.core.Array, np.ndarray or RawHDF5Dataset (this is a custom type)
        Raw XRF map represented as Dask array, numpy array or reference to a dataset in
        HDF5 file. The XRF map must have dimensions `(ny, nx, ne)`, where `ny` and `nx`
        define image size and `ne` is the number of spectrum points
    data_sel_indices: tuple
        tuple `(n_start, n_end)` which defines the indices along axis 2 of `data` array
        that are used for fitting. Note that `ne` (in `data`) and `ne_model` (in `matv`)
        are not equal. But `n_end - n_start` MUST be equal to `ne_model`! Indexes
        `n_start .. n_end - 1` will be selected from each pixel.
    matv: array
        Matrix of spectra of the selected elements (emission lines). Shape=(ne_model, n_lines)
    snip_param: dict
        Dictionary of parameters forwarded to 'snip' method for background removal.
        Keys: `e_offset`, `e_linear`, `e_quadratic` (parameters of the energy axis approximation),
        `b_width` (width of the window that defines resolution of the snip algorithm).
    use_snip: bool, optional
        enable/disable background removal using snip algorithm
    chunk_pixels: int
        The number of pixels in a single chunk. The XRF map will be rechunked so that
        each block contains approximately `chunk_pixels` pixels and contain all `ne`
        spectrum points for each pixel.
    n_chunks_min: int
        Minimum number of chunks. The algorithm will try to split the map into the number
        of chunks equal or greater than `n_chunks_min`.
    progress_bar: callable or None
        reference to the callable object that implements progress bar. The example of
        such a class for progress bar object is `TerminalProgressBar`.
    client: dask.distributed.Client or None
        Dask client. If None, then local client will be created

    Returns
    -------
    results: ndarray
        array with fitting results. Shape: `(ny, nx, ne_model + 4)`. For each pixel
        the output data contains: `ne_model` values that represent area under the emission
        line spectra; background area (only in the selected energy range), error (R-factor),
        total count in the selected energy range, total count of the full experimental spectrum.
    """

    logger.info("Starting single-pixel fitting ...")

    data, file_obj = _prepare_xrf_map(data, chunk_pixels=chunk_pixels, n_chunks_min=n_chunks_min)

    if client is None:
        client = Client(processes=True, silence_logs=logging.ERROR)
        client_is_local = True
    else:
        client_is_local = False

    n_workers = len(client.scheduler_info()["workers"])
    logger.info(f"Dask distributed client: {n_workers} workers")

    result_fut = da.map_blocks(_fit_xrf_block, data,
                               # Parameters of the '_fit_xrf_block' function
                               data_sel_indices=data_sel_indices,
                               matv=matv,
                               snip_param=snip_param,
                               use_snip=use_snip,
                               # Output data type
                               dtype="float")

    # Call the progress monitor
    wait_and_display_progress(result_fut, progress_bar)

    result = result_fut.compute(scheduler=client)

    if client_is_local:
        client.close()

    return result
