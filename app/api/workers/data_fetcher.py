import time
import logging
import os
import re
from dask.utils import memory_repr
import dask.array as da
import numpy as np
import xarray as xr
import zarr
import fsspec
import datashader
import hvplot.xarray  # noqa
import pandas as pd
from dask_kubernetes import KubeCluster, make_pod_spec
from dask.distributed import Client
from .models import OOIDataset

logger = logging.getLogger(__name__)
logging.root.setLevel(level=logging.INFO)


# ------------------ Helper Functions ------------------------
def _nan_to_nulls(values):
    """Converts nan to null values"""
    arr = np.nan_to_num(values, nan=-999999)
    return np.where(arr == -999999, None, arr)


def setup_params(axis_params):
    parameters = [v for v in set(axis_params.values()) if v]
    if "time" not in parameters:
        parameters = parameters + ["time"]

    return parameters


def _merge_datasets(data_list: dict, start_dt: str, end_dt: str) -> xr.Dataset:
    """Merges all dataset in data_list together into a single one"""
    # Merge data_list
    # --- This way of merging is for simple data only! ---
    # Align time based on request start and end datetime string
    time_defaults = {
        'units': 'seconds since 1900-01-01 00:00:00',
        'calendar': 'gregorian',
    }
    t_range, _, _ = xr.coding.times.encode_cf_datetime(
        [pd.to_datetime(start_dt), pd.to_datetime(end_dt)], **time_defaults
    )
    new_time = da.arange(*t_range).map_blocks(
        xr.coding.times.decode_cf_datetime, **time_defaults
    )
    dslist = [_interp_ds(ds, new_time) for _, ds in data_list.items()]
    # --- Done one way of merging ---

    merged = xr.merge(dslist, combine_attrs="no_conflicts").unify_chunks()

    return merged


def _plot_merged_dataset(
    merged: xr.Dataset,
    axis_params: dict,
    shade_threshold: int = 500000,
    plot_size: tuple = (888, 450),
) -> dict:
    """Use hvplot to plot the dataset and parse the plot dataframe"""

    def _change_z(k):
        if k == 'z':
            return 'color'
        return k

    rasterize = True if len(merged.time) > shade_threshold else False

    plot_params = {_change_z(k): v for k, v in axis_params.items()}

    # To filter resulting dataframe only grab non-empty parameters
    column_filter = [v for k, v in plot_params.items() if v]

    if plot_params["color"]:
        color_column = (
            f"{plot_params['x']}_{plot_params['y']} {plot_params['color']}"
        )
        plot = merged.hvplot.scatter(
            rasterize=rasterize,
            width=plot_size[0],
            height=plot_size[1],
            **plot_params,
        )
    elif rasterize:
        color_column = (
            f"{plot_params['x']}_{plot_params['y']} {plot_params['y']}"
        )
        plot = merged.hvplot.scatter(
            rasterize=True,
            width=plot_size[0],
            height=plot_size[1],
            aggregator=datashader.mean(column=plot_params["y"]),
            colorbar=False,
            **plot_params,
        )
        # Add the third column when it's shaded but only 2 params
        column_filter = column_filter + [color_column]
    else:
        color_column = None
        plot = merged.hvplot.scatter(
            rasterize=False,
            width=plot_size[0],
            height=plot_size[1],
            color="blue",
            **{k: v for k, v in plot_params.items() if k != "color"},
        )

    df = plot[()].dframe()
    if "time" in df:
        df.loc[:, "time"] = df["time"].astype(str)

    if plot_params["color"]:
        final_df = df.rename(columns={color_column: plot_params['color']})
    else:
        final_df = df[column_filter]

    final_dct = final_df.to_dict(orient='list')
    final_dct = {
        var: _nan_to_nulls(values).tolist()
        for var, values in final_dct.items()
    }

    return final_dct, rasterize, color_column


def fetch_zarr(zarr_url, storage_options={'anon': True}):
    zg = zarr.open_consolidated(
        fsspec.get_mapper(zarr_url, **storage_options), mode='r'
    )
    dimensions = {}
    variable_arrays = {}
    for k, a in zg.arrays():
        if k in a.attrs['_ARRAY_DIMENSIONS']:
            dimensions[k] = a.attrs['_ARRAY_DIMENSIONS']
        else:
            variable_arrays[k] = a.attrs['_ARRAY_DIMENSIONS']
    return zg, dimensions, variable_arrays


def _interp_ds(
    ds: xr.Dataset, new_time: pd.DatetimeIndex, method: str = 'nearest'
) -> xr.Dataset:
    return ds.interp(time=new_time).interpolate_na(
        dim='time', method=method, fill_value="extrapolate"
    )


def _clean_pod_spec(pod_spec):
    """Cleans pod specification for k8s"""
    # Set default API Version and kind
    setattr(pod_spec, 'api_version', 'v1')
    setattr(pod_spec, 'kind', 'Pod')
    # Cleans memory values for resources
    for c in pod_spec.spec.containers:
        for attr in c.resources.attribute_map.values():
            attr_val = getattr(c.resources, attr)
            val = attr_val['memory']
            val = val.replace("GB", "G")
            attr_val.update({'memory': val})
            setattr(c.resources, attr, attr_val)
    return pod_spec


def determine_workers(
    max_mem_size: int,
    memory_limit: int = 16,
    cpu_limit: int = 2,
    image_repo: str = 'cormorack',
    image_name: str = 'cava-dask',
    image_tag: str = '20210610',
) -> dict:
    """
    Determine dask worker spec and cluster size,
    based on total requested data size

    Parameters
    ----------
    max_mem_size: int
        Max memory requirement for the amount of data requested
    memory_limit: int
        Memory limit for the dask worker, as well max machine memory
    cpu_limit: int
        CPU limit for the dask worker
    image_repo: str
        Docker image repository for dask worker
    image_name:
        Docker image name for dask worker
    image_tag:
        Docker image tag for dask worker

    Returns
    -------
    dict
        Dictionary containing the pod_spec,
        and min, max for number of workers

    """
    max_workers = int(np.ceil(max_mem_size / memory_limit))
    min_workers = int(np.ceil(max_workers / 10))
    image = f"{image_repo}/{image_name}:{image_tag}"

    # Determine the memory and cpu request sizes
    k8s_mem = memory_limit / 2
    k8s_cpu = cpu_limit / 2
    if max_mem_size < memory_limit:
        k8s_mem = max_mem_size
        k8s_cpu = k8s_cpu / 2

    pod_spec = make_pod_spec(
        image=image,
        labels={'app.kubernetes.io/component': 'cava-dask'},
        memory_limit=f'{memory_limit}GB',
        memory_request=f'{k8s_mem}GB',
        cpu_limit=str(cpu_limit),
        cpu_request=str(k8s_cpu),
        extra_pod_config={
            'nodeSelector': {'kops.k8s.io/instancegroup': 'compute'},
            'restartPolicy': 'Never',
        },
        extra_container_config={
            'imagePullPolicy': 'IfNotPresent',
            'name': 'cava-dask',
        },
        threads_per_worker=2,
    )

    cleaned_spec = _clean_pod_spec(pod_spec)
    return {
        'min_workers': min_workers,
        'max_workers': max_workers,
        'pod_spec': cleaned_spec,
    }


def get_delayed_ds(
    request_params: list, axis_params: dict, include_dataset: bool = True
) -> dict:
    """Fetches the OOIDataset Delayed Object"""
    parameters = setup_params(axis_params)
    if "time" not in parameters:
        parameters.append("time")

    ds_list = {}
    for dataset_id in request_params:
        ooids = OOIDataset(dataset_id)[parameters]
        total_size = np.sum([v.nbytes for v in ooids.variables.values()])
        ds_list[dataset_id] = {
            'total_size': total_size,
        }
        if include_dataset:
            ds_list[dataset_id].update({'dataset': ooids})
    return ds_list


def fetch(
    self,
    request_params,
    axis_params,
    start_dt,
    end_dt,
    download,
    download_format,
    status_dict,
):
    self.update_state(
        state="PROGRESS",
        meta=status_dict,
    )
    ds_list = get_delayed_ds(request_params, axis_params)

    status_dict.update({"msg": f"{len(request_params)} datasets requested."})
    self.update_state(state="PROGRESS", meta=status_dict)

    max_data_size = np.sum([v['total_size'] for v in ds_list.values()])
    max_mem_size = max_data_size / 1024 ** 3

    dask_spec = {'min_workers': 1, 'max_workers': 2}
    data_threshold = os.environ.get('DATA_THRESHOLD', 50)

    client = None
    cluster = None

    if max_mem_size > data_threshold:
        image_repo, image_name, image_tag = (
            'cormorack',
            'cava-dask',
            '20210610',
        )
        desired_image = os.environ.get(
            "DASK_DOCKER_IMAGE", f"{image_repo}/{image_name}:{image_tag}"
        )
        match = re.match(r"(.+)/(.+):(.+)", desired_image)
        if match is not None:
            image_repo, image_name, image_tag = match.groups()
        dask_spec = determine_workers(
            max_mem_size,
            image_repo=image_repo,
            image_name=image_name,
            image_tag=image_tag,
        )

        status_dict.update(
            {
                "msg": f"Setting up distributed computing cluster. Max data size: {memory_repr(max_data_size)}"
            }
        )
        self.update_state(state="PROGRESS", meta=status_dict)
        cluster = KubeCluster(
            dask_spec['pod_spec'],
            n_workers=dask_spec['min_workers'],
        )
        cluster.adapt(
            minimum=dask_spec['min_workers'], maximum=dask_spec['max_workers']
        )
        client = Client(cluster)
    # TODO: Need to add other parameters for multidimensional
    # need a check for nutnr,pco2,ph,optaa add int_ctd_pressure
    # parameters.append("int_ctd_pressure")

    # for spikr
    # parameters.append("spectra")
    status_dict.update({"msg": "Retrieving data from zarr store ..."})
    self.update_state(state="PROGRESS", meta=status_dict)
    data_list = {
        k: v['dataset'].sel(time=(start_dt, end_dt)).dataset
        for k, v in ds_list.items()
    }

    status_dict.update({"msg": "Validating datasets..."})
    self.update_state(state="PROGRESS", meta=status_dict)
    if any(True for v in data_list.values() if v is None):
        # Checks if data_list is None
        status_dict.update(
            {"msg": "One of the dataset does not contain data."}
        )
        self.update_state(state="PROGRESS", meta=status_dict)
        time.sleep(2)
        result = None
    elif any(True for v in data_list.values() if len(v.time) == 0):
        empty_streams = []
        for k, v in data_list.items():
            if len(v.time) == 0:
                empty_streams.append(k)
        # Checks if data_list is None
        status_dict.update(
            {"msg": f"Empty data stream(s) found: {','.join(empty_streams)}."}
        )
        self.update_state(state="PROGRESS", meta=status_dict)
        time.sleep(2)
        status_dict.update(
            {
                "msg": "Plot creation is not possible with specified parameters. Please try again."
            }
        )
        self.update_state(state="PROGRESS", meta=status_dict)
        time.sleep(2)
        result = None
    else:
        total_requested_size = np.sum(
            np.fromiter((v.nbytes for v in data_list.values()), dtype=int)
        )
        status_dict.update(
            {
                "msg": f"There are {memory_repr(total_requested_size)} of data to be processed."
            }
        )
        self.update_state(state="PROGRESS", meta=status_dict)
        if len(data_list.keys()) > 1:
            merged = _merge_datasets(data_list, start_dt, end_dt)
        else:
            merged = next(ds for _, ds in data_list.items())

        data_count = len(merged.time)

        if data_count == 0:
            status_dict.update(
                {"msg": "Merged dataset does not contain data."}
            )
            self.update_state(state="PROGRESS", meta=status_dict)
            result = None
        else:
            status_dict.update({"msg": "Plotting merged datasets..."})
            self.update_state(state="PROGRESS", meta=status_dict)
            # Swapping dimensions for plotting to work if time is not
            # an axis selection
            if axis_params["x"] != "time":
                merged = merged.swap_dims({"time": axis_params['x']})
            # Shading process
            final_dct, shaded, color_column = _plot_merged_dataset(
                merged, axis_params
            )
            x = final_dct.get(axis_params['x'], [])
            y = final_dct.get(axis_params['y'], [])
            z = []
            if axis_params['z']:
                z = final_dct.get(axis_params['z'], np.array([]))
            elif shaded:
                z = final_dct.get(color_column, np.array([]))

            result = (
                {
                    "x": x,
                    "y": y,
                    "z": z,
                    "count": data_count,
                    "shaded": shaded,
                },
            )
        logger.info("Result done.")
    # ================ End Compute results ========================

    if client is not None:
        # Cleans up dask
        client.close()

    if cluster is not None:
        cluster.close()
    return result
