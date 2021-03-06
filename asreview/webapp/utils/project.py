from copy import deepcopy
import json
import logging
import shlex
import shutil
import subprocess

import numpy as np
from flask import current_app as app

from asreview import __version__ as asreview_version
from asreview.webapp.sqlock import SQLiteLock
from asreview.webapp.utils.paths import get_project_file_path, get_state_path
from asreview.webapp.utils.paths import get_kwargs_path
from asreview.webapp.utils.paths import asreview_path
from asreview.webapp.utils.paths import get_lock_path
from asreview.config import LABEL_NA
from asreview.webapp.utils.io import read_pool, write_pool, read_label_history
from asreview.webapp.utils.io import write_label_history, read_proba, read_data
from asreview.webapp.utils.io import read_current_labels


def init_project(
        project_id,
        project_name=None,
        project_description=None,
        project_authors=None
):
    """Initialize the necessary files specific to the web app."""

    if not project_id and not isinstance(project_id, str) \
            and len(project_id) >= 3:
        raise ValueError("Project name can't be None or empty string")

    # get the directory with the projects
    project_dir = asreview_path() / project_id

    if project_dir.exists():
        raise ValueError("Project already exists")

    try:
        project_dir.mkdir()

        fp_data = project_dir / "data"
        fp_data.mkdir()

        # create a file with project info
        with open(get_project_file_path(project_id), "w") as fp:
            json.dump({
                'version': asreview_version,  # todo: Fail without git?
                'id': project_id,
                'name': project_name,
                'description': project_description,
                'authors': project_authors
            }, fp)

        asr_kwargs = deepcopy(app.config['asr_kwargs'])  # remove config
        with open(get_kwargs_path(project_id), "w") as fp:
            json.dump(asr_kwargs, fp)

        # make a copy of the arguments to the state file
        asr_kwargs['state_file'] = str(get_state_path(project_id))

    except Exception as err:
        # remove all generated folders and raise error
        shutil.rmtree(project_dir)
        raise err


def add_dataset_to_project(project_id, file_name):
    """Add file path to the project file.

    Add file to data subfolder and fill the pool of iteration 0.
    """

    project_file_path = get_project_file_path(project_id)
    fp_lock = get_lock_path(project_id)

    with SQLiteLock(fp_lock, blocking=True, lock_name="active"):
        # open the projects file
        with open(project_file_path, "r") as f_read:
            project_dict = json.load(f_read)

        # add path to dict (overwrite if already exists)
        project_dict["dataset_path"] = file_name

        with open(project_file_path, "w") as f_write:
            json.dump(project_dict, f_write)

        # fill the pool of the first iteration
        pool_indices = read_data(project_id).record_ids
        np.random.shuffle(pool_indices)

        write_pool(project_id, pool_indices.tolist())

        # make a empty qeue for the items to label
        write_label_history(project_id, [])


def get_paper_data(project_id,
                   paper_id,
                   return_title=True,
                   return_authors=True,
                   return_abstract=True,
                   return_debug_label=False
                   ):
    """Get the title/authors/abstract for a paper."""
    as_data = read_data(project_id)
    record = as_data.record(int(paper_id))

    paper_data = {}
    if return_title and record.title is not None:
        paper_data['title'] = record.title
    if return_authors and record.authors is not None:
        paper_data['authors'] = record.authors
    if return_abstract and record.abstract is not None:
        paper_data['abstract'] = record.abstract

    # return the publication data if available
    if record.extra_fields.get("publish_time", None) is not None:
        paper_data['publish_time'] = \
            record.extra_fields.get("publish_time", None)

    # return the debug label
    if return_debug_label and \
            record.extra_fields.get("debug_label", None) is not None:
        paper_data['_debug_label'] = \
            int(record.extra_fields.get("debug_label", None))

    return paper_data


def get_instance(project_id):
    """Get a new instance to review.

    Arguments
    ---------
    project_id: str
        The id of the current project.
    """

    fp_lock = get_lock_path(project_id)

    with SQLiteLock(fp_lock, blocking=True, lock_name="active"):
        pool_idx = read_pool(project_id)

    logging.info(f"Requesting {pool_idx[0]} from project {project_id}")
    return pool_idx[0]


def get_statistics(project_id):
    fp_lock = get_lock_path(project_id)

    with SQLiteLock(fp_lock, blocking=True, lock_name="active"):
        # get the index of the active iteration
        label_history = read_label_history(project_id)
        current_labels = read_current_labels(
            project_id, label_history=label_history)

    n_since_last_inclusion = 0
    for _, inclusion in reversed(label_history):
        if inclusion == 1:
            break
        n_since_last_inclusion += 1

    n_included = len(np.where(current_labels == 1)[0])
    n_excluded = len(np.where(current_labels == 0)[0])
    n_papers = len(current_labels)
    stats = {
        "n_included": n_included,
        "n_excluded": n_excluded,
        "n_since_last_inclusion": n_since_last_inclusion,
        "n_papers": n_papers,
        "n_pool": n_papers - n_included - n_excluded
    }
    return stats


def export_to_string(project_id):
    fp_lock = get_lock_path(project_id)
    as_data = read_data(project_id)
    with SQLiteLock(fp_lock, blocking=True, lock_name="active"):
        proba = read_proba(project_id)
        if proba is None:
            proba = np.flip(np.arange(len(as_data)))
        else:
            proba = np.array(proba)
        labels = read_current_labels(project_id, as_data=as_data)

    pool_idx = np.where(labels == LABEL_NA)[0]
    one_idx = np.where(labels == 1)[0]
    zero_idx = np.where(labels == 0)[0]

    proba_order = np.argsort(-proba[pool_idx])
    ranking = np.concatenate(
        (one_idx, pool_idx[proba_order], zero_idx), axis=None)
    return as_data.to_csv(fp=None, labels=labels, ranking=ranking)


def label_instance(project_id, paper_i, label, retrain_model=True):
    """Label a paper after reviewing the abstract.

    """

    paper_i = int(paper_i)
    label = int(label)

    fp_lock = get_lock_path(project_id)

    with SQLiteLock(fp_lock, blocking=True, lock_name="active"):

        # get the index of the active iteration
        if int(label) in [0, 1]:
            move_label_from_pool_to_labeled(
                project_id, paper_i, label
            )
        else:
            move_label_from_labeled_to_pool(
                project_id, paper_i, label
            )

    if retrain_model:
        # Update the model (if it isn't busy).
        run_command = f"python -m asreview web_run_model '{project_id}'"
        subprocess.Popen(shlex.split(run_command))


def move_label_from_pool_to_labeled(project_id, paper_i, label):

    print(f"Move {paper_i} from pool to labeled")

    # load the papers from the pool
    pool_idx = read_pool(project_id)

    # Remove the paper from the pool.
    try:
        pool_idx.remove(int(paper_i))
    except (IndexError, ValueError):
        print(f"Failed to remove {paper_i} from the pool.")
        return

    write_pool(project_id, pool_idx)

    # Add the paper to the reviewed papers.
    labeled = read_label_history(project_id)
    labeled.append([int(paper_i), int(label)])
    write_label_history(project_id, labeled)


def move_label_from_labeled_to_pool(project_id, paper_i, label):

    print(f"Move {paper_i} from labeled to pool")

    # load the papers from the pool
    pool_list = read_pool(project_id)

    # Add the paper to the reviewed papers.
    labeled_list = read_label_history(project_id)

    labeled_list_new = []

    for item_id, item_label in labeled_list:

        item_id = int(item_id)
        item_label = int(item_label)

        if paper_i == item_id:
            pool_list.append(item_id)
        else:
            labeled_list_new.append([item_id, item_label])

    # write the papers to the label dataset
    write_pool(project_id, pool_list)

    # load the papers from the pool
    write_label_history(project_id, labeled_list_new)
