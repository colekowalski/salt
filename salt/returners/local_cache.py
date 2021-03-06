# -*- coding: utf-8 -*-
'''
Return data to local job cache

'''
from __future__ import absolute_import

# Import python libs
import errno
import logging
import os
import shutil
import time
import hashlib
import bisect

# Import salt libs
import salt.payload
import salt.utils
import salt.utils.jid

log = logging.getLogger(__name__)

# load is the published job
LOAD_P = '.load.p'
# the list of minions that the job is targeted to (best effort match on the master side)
MINIONS_P = '.minions.p'
# return is the "return" from the minion data
RETURN_P = 'return.p'
# out is the "out" from the minion data
OUT_P = 'out.p'
# endtime is the end time for a job, not stored as msgpack
ENDTIME = 'endtime'


def _job_dir():
    '''
    Return root of the jobs cache directory
    '''
    return os.path.join(__opts__['cachedir'],
                        'jobs')


def _jid_dir(jid):
    '''
    Return the jid_dir for the given job id
    '''
    jid = str(jid)
    jhash = getattr(hashlib, __opts__['hash_type'])(jid).hexdigest()
    return os.path.join(_job_dir(),
                        jhash[:2],
                        jhash[2:])


def _walk_through(job_dir):
    '''
    Walk though the jid dir and look for jobs
    '''
    serial = salt.payload.Serial(__opts__)

    for top in os.listdir(job_dir):
        t_path = os.path.join(job_dir, top)

        for final in os.listdir(t_path):
            load_path = os.path.join(t_path, final, LOAD_P)

            if not os.path.isfile(load_path):
                continue

            job = serial.load(salt.utils.fopen(load_path, 'rb'))
            jid = job['jid']
            yield jid, job, t_path, final


#TODO: add to returner docs-- this is a new one
def prep_jid(nocache=False, passed_jid=None):
    '''
    Return a job id and prepare the job id directory
    This is the function responsible for making sure jids don't collide (unless its passed a jid)
    So do what you have to do to make sure that stays the case
    '''
    if passed_jid is None:  # this can be a None of an empty string
        jid = salt.utils.jid.gen_jid()
    else:
        jid = passed_jid

    jid_dir_ = _jid_dir(jid)

    # make sure we create the jid dir, otherwise someone else is using it,
    # meaning we need a new jid
    try:
        os.makedirs(jid_dir_)
    except OSError:
        # TODO: some sort of sleep or something? Spinning is generally bad practice
        if passed_jid is None:
            return prep_jid(nocache=nocache)

    with salt.utils.fopen(os.path.join(jid_dir_, 'jid'), 'wb+') as fn_:
        fn_.write(jid)
    if nocache:
        with salt.utils.fopen(os.path.join(jid_dir_, 'nocache'), 'wb+') as fn_:
            fn_.write('')

    return jid


def returner(load):
    '''
    Return data to the local job cache
    '''
    serial = salt.payload.Serial(__opts__)

    # if a minion is returning a standalone job, get a jobid
    if load['jid'] == 'req':
        load['jid'] = prep_jid(nocache=load.get('nocache', False))

    jid_dir = _jid_dir(load['jid'])
    if os.path.exists(os.path.join(jid_dir, 'nocache')):
        return

    hn_dir = os.path.join(jid_dir, load['id'])

    try:
        os.makedirs(hn_dir)
    except OSError as err:
        if err.errno == errno.EEXIST:
            # Minion has already returned this jid and it should be dropped
            log.error(
                'An extra return was detected from minion {0}, please verify '
                'the minion, this could be a replay attack'.format(
                    load['id']
                )
            )
            return False
        elif err.errno == errno.ENOENT:
            log.error(
                'An inconsistency occurred, a job was received with a job id '
                'that is not present in the local cache: {jid}'.format(**load)
            )
            return False
        raise

    serial.dump(
        load['return'],
        # Use atomic open here to avoid the file being read before it's
        # completely written to. Refs #1935
        salt.utils.atomicfile.atomic_open(
            os.path.join(hn_dir, RETURN_P), 'w+b'
        )
    )

    if 'out' in load:
        serial.dump(
            load['out'],
            # Use atomic open here to avoid the file being read before
            # it's completely written to. Refs #1935
            salt.utils.atomicfile.atomic_open(
                os.path.join(hn_dir, OUT_P), 'w+b'
            )
        )


def save_load(jid, clear_load):
    '''
    Save the load to the specified jid
    '''
    jid_dir = _jid_dir(jid)

    serial = salt.payload.Serial(__opts__)

    # if you have a tgt, save that for the UI etc
    if 'tgt' in clear_load:
        ckminions = salt.utils.minions.CkMinions(__opts__)
        # Retrieve the minions list
        minions = ckminions.check_minions(
                clear_load['tgt'],
                clear_load.get('tgt_type', 'glob')
                )
        # save the minions to a cache so we can see in the UI
        try:
            serial.dump(
                minions,
                salt.utils.fopen(os.path.join(jid_dir, MINIONS_P), 'w+b')
                )
        except IOError:
            log.warning('Could not write job cache file for minions: {0}'.format(minions))

    # Save the invocation information
    try:
        if not os.path.exists(jid_dir):
            os.makedirs(jid_dir)
        serial.dump(
            clear_load,
            salt.utils.fopen(os.path.join(jid_dir, LOAD_P), 'w+b')
            )
    except IOError as exc:
        log.warning('Could not write job invocation cache file: {0}'.format(exc))


def get_load(jid):
    '''
    Return the load data that marks a specified jid
    '''
    jid_dir = _jid_dir(jid)
    load_fn = os.path.join(jid_dir, LOAD_P)
    if not os.path.exists(jid_dir) or not os.path.exists(load_fn):
        return {}
    serial = salt.payload.Serial(__opts__)

    ret = serial.load(salt.utils.fopen(os.path.join(jid_dir, LOAD_P), 'rb'))

    minions_path = os.path.join(jid_dir, MINIONS_P)
    if os.path.isfile(minions_path):
        ret['Minions'] = serial.load(salt.utils.fopen(minions_path, 'rb'))

    return ret


def get_jid(jid):
    '''
    Return the information returned when the specified job id was executed
    '''
    jid_dir = _jid_dir(jid)
    serial = salt.payload.Serial(__opts__)

    ret = {}
    # Check to see if the jid is real, if not return the empty dict
    if not os.path.isdir(jid_dir):
        return ret
    for fn_ in os.listdir(jid_dir):
        if fn_.startswith('.'):
            continue
        if fn_ not in ret:
            retp = os.path.join(jid_dir, fn_, RETURN_P)
            outp = os.path.join(jid_dir, fn_, OUT_P)
            if not os.path.isfile(retp):
                continue
            while fn_ not in ret:
                try:
                    ret_data = serial.load(
                        salt.utils.fopen(retp, 'rb'))
                    ret[fn_] = {'return': ret_data}
                    if os.path.isfile(outp):
                        ret[fn_]['out'] = serial.load(
                            salt.utils.fopen(outp, 'rb'))
                except Exception as exc:
                    if 'Permission denied:' in str(exc):
                        raise
    return ret


def get_jids():
    '''
    Return a dict mapping all job ids to job information
    '''
    ret = {}
    for jid, job, _, _ in _walk_through(_job_dir()):
        ret[jid] = salt.utils.jid.format_jid_instance(jid, job)

        if __opts__.get('job_cache_store_endtime'):
            endtime = get_endtime(jid)
            if endtime:
                ret[jid]['EndTime'] = endtime

    return ret


def get_jids_filter(count, filter_find_job=True):
    '''
    Return a list of all jobs information filtered by the given criteria.
    :param int count: show not more than the count of most recent jobs
    :param bool filter_find_jobs: filter out 'saltutil.find_job' jobs
    '''
    keys = []
    ret = []
    for jid, job, _, _ in _walk_through(_job_dir()):
        job = salt.utils.jid.format_jid_instance_ext(jid, job)
        if filter_find_job and job['Function'] == 'saltutil.find_job':
            continue
        i = bisect.bisect(keys, jid)
        if len(keys) == count and i == 0:
            continue
        keys.insert(i, jid)
        ret.insert(i, job)
        if len(keys) > count:
            del keys[0]
            del ret[0]
    return ret


def clean_old_jobs():
    '''
    Clean out the old jobs from the job cache
    '''
    if __opts__['keep_jobs'] != 0:
        cur = time.time()
        jid_root = _job_dir()

        if not os.path.exists(jid_root):
            return

        for top in os.listdir(jid_root):
            t_path = os.path.join(jid_root, top)
            for final in os.listdir(t_path):
                f_path = os.path.join(t_path, final)
                jid_file = os.path.join(f_path, 'jid')
                if not os.path.isfile(jid_file):
                    # No jid file means corrupted cache entry, scrub it
                    shutil.rmtree(f_path)
                else:
                    jid_ctime = os.stat(jid_file).st_ctime
                    hours_difference = (cur - jid_ctime) / 3600.0
                    if hours_difference > __opts__['keep_jobs']:
                        shutil.rmtree(f_path)


def update_endtime(jid, time):
    '''
    Update (or store) the end time for a given job

    Endtime is stored as a plain text string
    '''
    jid_dir = _jid_dir(jid)
    try:
        if not os.path.exists(jid_dir):
            os.makedirs(jid_dir)
        with salt.utils.fopen(os.path.join(jid_dir, ENDTIME), 'w') as etfile:
            etfile.write(time)
    except IOError as exc:
        log.warning('Could not write job invocation cache file: {0}'.format(exc))


def get_endtime(jid):
    '''
    Retrieve the stored endtime for a given job

    Returns False if no endtime is present
    '''
    jid_dir = _jid_dir(jid)
    etpath = os.path.join(jid_dir, ENDTIME)
    if not os.path.exists(etpath):
        return False
    with salt.utils.fopen(etpath, 'r') as etfile:
        endtime = etfile.read().strip('\n')
    return endtime
