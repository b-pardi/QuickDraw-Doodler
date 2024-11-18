from quickdraw import QuickDrawData, QuickDrawDataGroup
from itertools import islice
import os
import numpy as np
import matplotlib.pyplot as plt
from datasets import load_dataset

from utils.types import DataMode
from utils.image_processing import vector_to_raster, full_strokes_to_vector_images

data_dir = "quickdraw_data"  # Directory to save the data
os.makedirs(data_dir, exist_ok=True)

def list_all_classes():
    qdd = QuickDrawData()
    print(qdd.drawing_names)

def test_display_img(img, label, idx):
    plt.imshow(img, cmap='gray')
    plt.title(f"Label: {label}")
    plt.axis('off')
    plt.savefig(f"output/figs/{label}-{idx}.png")
    #plt.show()

def is_data_downloaded(label_path, num_samples_per_class, data_mode):
    # check if data path already exists
    if os.path.exists(label_path):
        
        # if data path exists, check if it's the same number of samples currently passed in
        print(label_path)
        preexisting_data = np.load(label_path, allow_pickle=True)
        if preexisting_data.shape[0] == num_samples_per_class:
            print(f"Found {num_samples_per_class} {data_mode.value} doodles for {label_path} already downloaded.")
            return True
        else:
            print(f"Found {preexisting_data.shape[0]} {data_mode.value} doodles for {label_path}, but num_samples given is {num_samples_per_class}, updating download...")
            return False
    else:
        print(f"No {data_mode.value} samples for {label_path}, downloading now...")
        return False

def download_img_data(subset_labels, data_mode, num_samples_per_class, data_dir="quickdraw_data/"):
    """
    Download a specified number of samples for each class and save as .npy files.
    Checks if already download before with the given number of samples and data mode.

    Args:
        classes (list of str): List of classes to download.
        data_mode (DataMode): Mode for downloading data ('full' or 'simplified').
        num_samples (int): Number of samples to download per class.
        data_dir (str): File path
    """
    data_dir += data_mode.value
    os.makedirs(data_dir, exist_ok=True)
    if data_mode == DataMode.SIMPLIFIED:
        out_size = 256
    elif data_mode == DataMode.REDUCED:
        out_size = 28
    else:
        print("Error: this function only applies to simplified or reduced data_modes")
        return
    
    for label in subset_labels:
        label_path = os.path.join(data_dir, f"{label}.npy") # combine label and data directory to make data file path

        # see if current label data downloaded with current data_mode and num_samples_per_class
        if is_data_downloaded(label_path, num_samples_per_class, data_mode):
            continue

        # initialize group for current label
        qddg = QuickDrawDataGroup(label, recognized=True, max_drawings=num_samples_per_class)

        strokes = []
        for drawing in qddg.drawings:
            strokes.append(drawing.strokes)

        if data_mode == DataMode.SIMPLIFIED:
            images = vector_to_raster(strokes, in_size=256, out_size=out_size, line_diameter=8, padding=8) # convert vector stroke data to images
        elif data_mode == DataMode.REDUCED:
            images = vector_to_raster(strokes, in_size=256, out_size=out_size, line_diameter=2, padding=2) # convert vector stroke data to images
        images = images.astype(np.float32) / 255.0 # normalize images
        print(images.shape)
        # save data to appropriate dir based on label name and data mode
        np.save(label_path, images)
        print(label_path)

        print(f"**SAVED {num_samples_per_class} of {data_mode.value} {label} samples.")

def download_stroke_data(subset_labels, data_mode, num_samples_per_class, data_dir="quickdraw_data/", streaming_mode=True, cache_dir=".hfcache"):
    """
    Get all samples of a class one at a time, then grab num_samples_per_class,
    remove unnecessary features, and save as .npy

    sample stroke data:
    [ 
        [  // First stroke 
            [x0, x1, x2, x3, ...],
            [y0, y1, y2, y3, ...],
            [t0, t1, t2, t3, ...]
        ],
        [  // Second stroke
            [x0, x1, x2, x3, ...],
            [y0, y1, y2, y3, ...],
            [t0, t1, t2, t3, ...]
        ],
        ... // Additional strokes
    ]

    Args:
        subset_labels (list of str): List of classes to load.
        data_mode (DataMode): Mode for downloading data ('full' or 'simplified').
        num_samples_per_class (int): Number of samples to download per class.
        data_dir (str): Directory path to save the data.
    """
    data_dir = os.path.join(data_dir, data_mode.value)
    os.makedirs(data_dir, exist_ok=True)
    if not streaming_mode:
        os.makedirs(cache_dir, exist_ok=True)

    # dataset loading takes time, check if data downloaded beforehand
    missing_labels = []
    for label in subset_labels: # iterate through labels
        label_path = os.path.join(data_dir, f"{label}.npy") # combine label and data directory to make data file path
        
        if not is_data_downloaded(label_path, num_samples_per_class, data_mode):
            missing_labels.append(label)
    
    # return if all data previously downloaded
    if not missing_labels:
        return

    # where images will be downloaded from
    base_url = "https://storage.googleapis.com/quickdraw_dataset/full/raw/{}.ndjson"
    
    # load iterable dataset to avoid downloading whole thing
    dataset = load_dataset(
        "json",
        data_files={label: base_url.format(label) for label in subset_labels},
        streaming=streaming_mode,
        cache_dir=cache_dir
    )

    for label in missing_labels: # iterate through labels
        label_path = os.path.join(data_dir, f"{label}.npy") # combine label and data directory to make data file path
        
        sample_idx = 0
        print(f"Assembling {label} data...")
        drawings_arr = np.empty(num_samples_per_class, dtype=object)
        for sample in dataset[label]: # iterate through samples in labels
            # ensure sample recognized, there's at least one stroke, and all strokes have x, y, and t
            if not (sample["recognized"] and len(sample["drawing"]) >= 1  and all(len(stroke) == 3 for stroke in sample["drawing"])):
                continue

            drawing_data = sample['drawing']

            # includes pen up points that will be inserted
            total_points = sum(len(stroke[0]) for stroke in drawing_data) + (len(drawing_data) - 1)

            x = np.empty(total_points, dtype=np.float32)
            y = np.empty(total_points, dtype=np.float32)
            t = np.empty(total_points, dtype=np.int32)
            p = np.empty(total_points, dtype=np.uint8)

            point_idx = 0
            for stroke_idx, stroke in enumerate(drawing_data):
                num_points_in_stroke = len(stroke[0])

                # insert a pen-up point if not the first stroke
                if stroke_idx > 0:
                    # Insert pen-up point (repeats last point of previous stroke)
                    x[point_idx] = x[point_idx - 1]
                    y[point_idx] = y[point_idx - 1]
                    t[point_idx] = t[point_idx - 1] + 1  # increment timestamp
                    p[point_idx] = 2  # Pen up
                    point_idx += 1

                # get current stroke points in arr
                x_stroke = np.array(stroke[0], dtype=np.float32)
                y_stroke = np.array(stroke[1], dtype=np.float32)
                t_stroke = np.array(stroke[2], dtype=np.int32)

                # Pen state array for the current stroke
                p_stroke = np.ones(num_points_in_stroke, dtype=np.uint8)  # pen down
                if stroke_idx == 0:
                    p_stroke[0] = 0  # pen start
                else:
                    p_stroke[0] = 1  # pen down after pen up

                # assign stroke points to collective stroke arrays
                x[point_idx:point_idx + num_points_in_stroke] = x_stroke
                y[point_idx:point_idx + num_points_in_stroke] = y_stroke
                t[point_idx:point_idx + num_points_in_stroke] = t_stroke
                p[point_idx:point_idx + num_points_in_stroke] = p_stroke

                point_idx += num_points_in_stroke
            
            # last pen state is pen end
            p[point_idx - 1] = 3

            drawings_arr[sample_idx] = np.stack([x[:point_idx], y[:point_idx], t[:point_idx], p[:point_idx]])
            
            sample_idx+=1
            if sample_idx >= num_samples_per_class:
                break
        
        # saved class data as npy
        np.save(label_path, drawings_arr)

        print(f"**SAVED {num_samples_per_class} of {data_mode.value} {label} samples.")


def load_simplified_data(subset_labels, data_mode, num_samples_per_class, data_dir="quickdraw_data/"):
    """
    Load and prepare data from .npy files for the specified classes.

    Args:
        subset_labels (list of str): List of class names to load (e.g., ['cat', 'dog']).
        data_mode (DataMode): Mode for using data ('reduced' or 'simplified', 'full' is also a data mode but not applicable here).
        num_samples_per_class (int): how many to samples each class should have
        data_dir (str): Directory where data can be found, defaults to 'quickdraw_data', same default as download
        
    Returns:
        images (np.ndarray): np arr of shape of all images.
        labels (np.1darray): array of index labels corresponding to subset labels list.
    """
    print("**LOADING DATA FROM .npy FILES")
    total_samples = num_samples_per_class * len(subset_labels)
    
    # image dimensions based on datamode
    image_data_dim = (total_samples, 28,28) if data_mode == DataMode.REDUCED else (total_samples, 256,256)

    # allocate arrays for images/labels and optionally for strokes
    images = np.empty(image_data_dim, dtype=np.float32)
    labels = np.empty(total_samples, dtype=np.uint8)

    for i, label in enumerate(subset_labels):
        # get file path and load .npy file
        cur_data_dir = os.path.join(data_dir, data_mode.value, f"{label}.npy")
        data = np.load(cur_data_dir, allow_pickle=True)
       
        # slice indices for this class
        start_idx = i * num_samples_per_class
        end_idx = start_idx + num_samples_per_class

        # load images and normalize pixel values to [0, 1] range
        images[start_idx:end_idx] = data[:num_samples_per_class]
        labels[start_idx:end_idx] = i # label images by index

    print(f"Loaded and prepared {total_samples} images with labels for model training (data mode: {data_mode})")
    return images, labels

def load_stroke_data(subset_labels, data_mode, num_samples_per_class, data_dir="quickdraw_data/"):
    """
    Load stroke data from a saved .npy file for a given label and data mode.

    Args:
        label (str): The class label to load (e.g., "cat", "tree").
        data_mode (str): The mode under which data was saved (e.g., "full" or "simplified").
        num_samples_per_class (int): number of data samples per label
        data_dir (str): Directory where data can be found, defaults to 'quickdraw_data', same default as download

    Returns:
        numpy.ndarray: A structured numpy array where each entry has fields `x`, `y`, and `t`, 
                       each storing a numpy array of stroke data for the given label.
    """
    total_samples = num_samples_per_class * len(subset_labels)

    # Initialize numpy arrays for drawings and labels
    drawings = np.empty(total_samples, dtype=object)
    labels = np.empty(total_samples, dtype=np.uint8)

    for i, label in enumerate(subset_labels):
        cur_data_dir = os.path.join(data_dir, data_mode.value, f"{label}.npy")
        data = np.load(cur_data_dir, allow_pickle=True)  # allow_pickle=True for variable-length arrays

        # slice indices for this class
        start_idx = i * num_samples_per_class
        end_idx = start_idx + num_samples_per_class

        # Insert data into the allocated numpy arrays
        drawings[start_idx:end_idx] = data[:num_samples_per_class]
        labels[start_idx:end_idx] = i

    print(f"Loaded and prepared {total_samples} drawings with labels for model training (data mode: {data_mode})")

    return drawings, labels

def local_normalize_stroke_data(data):
    norm_data = np.empty(data.shape[0], dtype=object)
    stats = []  # List to store min and max values for x, y, and t for each sample
    for i, sample in enumerate(data):
        x, y, t, p = sample
        x_min, x_max = x.min(), x.max()
        y_min, y_max = y.min(), y.max()
        t_min, t_max = t.min(), t.max()

        # edge cases if the min ever equals max (vert or hor lines)
        x_range = x_max - x_min if x_max - x_min != 0 else 1e-6
        y_range = y_max - y_min if y_max - y_min != 0 else 1e-6
        t_range = t_max - t_min if t_max - t_min != 0 else 1e-6

        # normalize x and y to edges of bbox (xmax and ymax)
        # x, y are normalized before finding deltas to ensure scale consistency with bbox
        # t normalized before deltas to ensure temproal dynamics are relative to other strokes, not other drawings
        x_norm = (x - x_min) / x_range
        y_norm = (y - y_min) / y_range
        t_norm = (t - t_min) / t_range

        # absolute values not necessary to process sequential inputs
        dx = np.diff(x_norm, prepend=x_norm[0]).astype(np.float32)
        dy = np.diff(y_norm, prepend=y_norm[0]).astype(np.float32)
        dt = np.diff(t_norm, prepend=t_norm[0]).astype(np.int16)

        norm_data[i] = np.stack([dx, dy, dt, p], axis=0)

        stats.append({'x_min': x_min, 'x_max': x_max, 'y_min': y_min, 'y_max': y_max, 't_min': t_min, 't_max': t_max})

    return norm_data, stats

def unnormalize_stroke_data(norm_data, stats):
    unnorm_data = np.empty(norm_data.shape[0], dtype=object)
    for i, sample in enumerate(norm_data):
        dx, dy, dt, p = sample[:, 0], sample[:, 1], sample[:, 2], sample[:, 3]
        x_min, x_max = stats[i]['x_min'], stats[i]['x_max']
        y_min, y_max = stats[i]['y_min'], stats[i]['y_max']
        t_min, t_max = stats[i]['t_min'], stats[i]['t_max']

        x_range = x_max - x_min if x_max - x_min != 0 else 1e-6
        y_range = y_max - y_min if y_max - y_min != 0 else 1e-6
        t_range = t_max - t_min if t_max - t_min != 0 else 1e-6

        # reconstruct normalized positions from deltas
        x_norm = np.cumsum(dx)
        y_norm = np.cumsum(dy)
        t_norm = np.cumsum(dt)

        # unnormalize the positions
        x = x_norm * x_range + x_min
        y = y_norm * y_range + y_min
        t = t_norm * t_range + t_min

        unnorm_data[i] = np.stack([x, y, t, p], axis=0)
        
    return unnorm_data
