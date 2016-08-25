#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
Search Architecture:
 - Have a list of accounts
 - Create an "overseer" thread
 - Search Overseer:
   - Tracks incoming new location values
   - Tracks "paused state"
   - During pause or new location will clears current search queue
   - Starts search_worker threads
 - Search Worker Threads each:
   - Have a unique API login
   - Listens to the same Queue for areas to scan
   - Can re-login as needed
   - Shares a global lock for map parsing
'''

import logging
import math
import json
import os
import random
import time
import geopy
import geopy.distance

from operator import itemgetter
from threading import Thread, Lock
from queue import Queue, Empty

from pgoapi import PGoApi
from pgoapi.utilities import f2i
from pgoapi import utilities as util
from pgoapi.exceptions import AuthException

from .models import parse_map, Pokemon
from .fakePogoApi import FakePogoApi
import terminalsize

log = logging.getLogger(__name__)

TIMESTAMP = '\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000'


def get_new_coords(init_loc, distance, bearing):
    """
    Given an initial lat/lng, a distance(in kms), and a bearing (degrees),
    this will calculate the resulting lat/lng coordinates.
    """
    R = 6378.1  # km radius of the earth
    bearing = math.radians(bearing)

    init_coords = [math.radians(init_loc[0]), math.radians(init_loc[1])]  # convert lat/lng to radians

    new_lat = math.asin(math.sin(init_coords[0]) * math.cos(distance / R) +
                        math.cos(init_coords[0]) * math.sin(distance / R) * math.cos(bearing)
                        )

    new_lon = init_coords[1] + math.atan2(math.sin(bearing) * math.sin(distance / R) * math.cos(init_coords[0]),
                                          math.cos(distance / R) - math.sin(init_coords[0]) * math.sin(new_lat)
                                          )

    return [math.degrees(new_lat), math.degrees(new_lon)]


def generate_location_steps(initial_loc, step_count, step_distance):
    # Bearing (degrees)
    NORTH = 0
    EAST = 90
    SOUTH = 180
    WEST = 270

    pulse_radius = step_distance            # km - radius of players heartbeat is 70m
    xdist = math.sqrt(3) * pulse_radius   # dist between column centers
    ydist = 3 * (pulse_radius / 2)          # dist between row centers

    yield (initial_loc[0], initial_loc[1], 0)  # insert initial location

    ring = 1
    loc = initial_loc
    while ring < step_count:
        # Set loc to start at top left
        loc = get_new_coords(loc, ydist, NORTH)
        loc = get_new_coords(loc, xdist / 2, WEST)
        for direction in range(6):
            for i in range(ring):
                if direction == 0:  # RIGHT
                    loc = get_new_coords(loc, xdist, EAST)
                if direction == 1:  # DOWN + RIGHT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist / 2, EAST)
                if direction == 2:  # DOWN + LEFT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist / 2, WEST)
                if direction == 3:  # LEFT
                    loc = get_new_coords(loc, xdist, WEST)
                if direction == 4:  # UP + LEFT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist / 2, WEST)
                if direction == 5:  # UP + RIGHT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist / 2, EAST)
                yield (loc[0], loc[1], 0)
        ring += 1


# Apply a location jitter
def jitterLocation(location=None, maxMeters=10):
    origin = geopy.Point(location[0], location[1])
    b = random.randint(0, 360)
    d = math.sqrt(random.random()) * (float(maxMeters) / 1000)
    destination = geopy.distance.distance(kilometers=d).destination(origin, b)
    return (destination.latitude, destination.longitude, location[2])


# gets the current time past the hour
def curSec():
    return (60 * time.gmtime().tm_min) + time.gmtime().tm_sec


# gets the diference between two times past the hour (in a range from -1800 to 1800)
def timeDif(a, b):
    dif = a - b
    if (dif < -1800):
        dif += 3600
    if (dif > 1800):
        dif -= 3600
    return dif


# binary search to get the lowest index of the item in Slist that has atleast time T
def SbSearch(Slist, T):
    first = 0
    last = len(Slist) - 1
    while first < last:
        mp = (first + last) // 2
        if Slist[mp]['time'] < T:
            first = mp + 1
        else:
            last = mp
    return first


# Thread to handle user input
def switch_status_printer(display_enabled, current_page):
    while True:
        # Wait for the user to press a key
        command = raw_input()

        if command == '':
            # Switch between logging and display.
            if display_enabled[0]:
                logging.disable(logging.NOTSET)
                display_enabled[0] = False
            else:
                logging.disable(logging.ERROR)
                display_enabled[0] = True
        elif command.isdigit():
                current_page[0] = int(command)


# Thread to print out the status of each worker
def status_printer(threadStatus, search_items_queue, db_updates_queue, wh_queue):
    display_enabled = [True]
    current_page = [1]
    logging.disable(logging.ERROR)

    # Start another thread to get user input
    t = Thread(target=switch_status_printer,
               name='switch_status_printer',
               args=(display_enabled, current_page))
    t.daemon = True
    t.start()

    while True:
        if display_enabled[0]:

            # Get the terminal size
            width, height = terminalsize.get_terminal_size()
            # Queue and overseer take 2 lines.  Switch message takes up 2 lines.  Remove an extra 2 for things like screen status lines.
            usable_height = height - 6
            # Prevent people running terminals only 6 lines high from getting a divide by zero
            if usable_height < 1:
                usable_height = 1

            # Create a list to hold all the status lines, so they can be printed all at once to reduce flicker
            status_text = []

            # Print the queue length
            if type(search_items_queue) is list:
                queue_status = ", ".join([str(queue.qsize()) for queue in search_items_queue])
            else:
                queue_status = search_items_queue.qsize()
            status_text.append('Queues: {} items, {} db updates, {} webhook'.format(queue_status, db_updates_queue.qsize(), wh_queue.qsize()))

            # Print status of overseer
            status_text.append('{} Overseer: {}'.format(threadStatus['Overseer']['method'], threadStatus['Overseer']['message']))

            # Calculate the total number of pages.  Subtracting 1 for the overseer.
            total_pages = math.ceil((len(threadStatus) - 1) / float(usable_height))

            # Prevent moving outside the valid range of pages
            if current_page[0] > total_pages:
                current_page[0] = total_pages
            if current_page[0] < 1:
                current_page[0] = 1

            # Calculate which lines to print
            start_line = usable_height * (current_page[0] - 1)
            end_line = start_line + usable_height
            current_line = 1

            # Print the worker status
            for item in sorted(threadStatus):
                if(threadStatus[item]['type'] == "Worker"):
                    current_line += 1

                    # Skip over items that don't belong on this page
                    if current_line < start_line:
                        continue
                    if current_line > end_line:
                        break

                    if 'skip' in threadStatus[item]:
                        status_text.append('{} - Success: {}, Failed: {}, No Items: {}, Skipped: {} - {}'.format(item, threadStatus[item]['success'], threadStatus[item]['fail'], threadStatus[item]['noitems'], threadStatus[item]['skip'], threadStatus[item]['message']))
                    else:
                        status_text.append('{} - Success: {}, Failed: {}, No Items: {} - {}'.format(item, threadStatus[item]['success'], threadStatus[item]['fail'], threadStatus[item]['noitems'], threadStatus[item]['message']))
            status_text.append('Page {}/{}.  Type page number and <ENTER> to switch pages.  Press <ENTER> alone to switch between status and log view'.format(current_page[0], total_pages))
            # Clear the screen
            os.system('cls' if os.name == 'nt' else 'clear')
            # Print status
            print "\n".join(status_text)
        time.sleep(1)


# The main search loop that keeps an eye on the over all process
def search_overseer_thread(args, new_location_queue, pause_bit, encryption_lib_path, db_updates_queue, wh_queue):

    log.info('Search overseer starting')

    search_items_queue = Queue()
    parse_lock = Lock()
    threadStatus = {}

    threadStatus['Overseer'] = {}
    threadStatus['Overseer']['message'] = "Initializing"
    threadStatus['Overseer']['type'] = "Overseer"
    threadStatus['Overseer']['method'] = "Hex Grid"

    if(args.print_status):
        log.info('Starting status printer thread')
        t = Thread(target=status_printer,
                   name='status_printer',
                   args=(threadStatus, search_items_queue, db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    # Create a search_worker_thread per account
    log.info('Starting search worker threads')
    for i, account in enumerate(args.accounts):
        log.debug('Starting search worker thread %d for user %s', i, account['username'])
        threadStatus['Worker {:03}'.format(i)] = {}
        threadStatus['Worker {:03}'.format(i)]['type'] = "Worker"
        threadStatus['Worker {:03}'.format(i)]['message'] = "Creating thread..."
        threadStatus['Worker {:03}'.format(i)]['success'] = 0
        threadStatus['Worker {:03}'.format(i)]['fail'] = 0
        threadStatus['Worker {:03}'.format(i)]['noitems'] = 0

        t = Thread(target=search_worker_thread,
                   name='search-worker-{}'.format(i),
                   args=(args, account, search_items_queue, parse_lock,
                         encryption_lib_path, threadStatus['Worker {:03}'.format(i)],
                         db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    # A place to track the current location
    current_location = False
    locations = []
    spawnpoints = set()

    # The real work starts here but will halt on pause_bit.set()
    while True:

        # paused; clear queue if needed, otherwise sleep and loop
        if pause_bit.is_set():
            if not search_items_queue.empty():
                try:
                    while True:
                        search_items_queue.get_nowait()
                except Empty:
                    pass
            threadStatus['Overseer']['message'] = "Scanning is paused"
            time.sleep(1)
            continue

        # If a new location has been passed to us, get the most recent one
        if not new_location_queue.empty():
            log.info('New location caught, moving search grid')
            try:
                while True:
                    current_location = new_location_queue.get_nowait()
            except Empty:
                pass

            # We (may) need to clear the search_items_queue
            if not search_items_queue.empty():
                try:
                    while True:
                        search_items_queue.get_nowait()
                except Empty:
                    pass

            # if we are only scanning for pokestops/gyms, then increase step radius to visibility range
            if args.no_pokemon:
                step_distance = 0.9
            else:
                step_distance = 0.07

            log.info('Scan Distance is %.2f km', step_distance)

            # update our list of coords
            locations = list(generate_location_steps(current_location, args.step_limit, step_distance))

            # repopulate our spawn points
            if args.spawnpoints_only:
                # We need to get all spawnpoints in range. This is a square 70m * step_limit * 2
                sp_dist = 0.07 * 2 * args.step_limit
                log.debug('Spawnpoint search radius: %f', sp_dist)
                # generate coords of the midpoints of each edge of the square
                south, west = get_new_coords(current_location, sp_dist, 180), get_new_coords(current_location, sp_dist, 270)
                north, east = get_new_coords(current_location, sp_dist, 0), get_new_coords(current_location, sp_dist, 90)
                # Use the midpoints to arrive at the corners
                log.debug('Searching for spawnpoints between %f, %f and %f, %f', south[0], west[1], north[0], east[1])
                spawnpoints = set((d['latitude'], d['longitude']) for d in Pokemon.get_spawnpoints(south[0], west[1], north[0], east[1]))
                if len(spawnpoints) == 0:
                    log.warning('No spawnpoints found in the specified area! (Did you forget to run a normal scan in this area first?)')

                def any_spawnpoints_in_range(coords):
                    return any(geopy.distance.distance(coords, x).meters <= 70 for x in spawnpoints)

                locations = [coords for coords in locations if any_spawnpoints_in_range(coords)]

            if len(locations) == 0:
                log.warning('Nothing to scan!')

        # If there are no search_items_queue either the loop has finished (or been
        # cleared above) -- either way, time to fill it back up
        if search_items_queue.empty():
            log.debug('Search queue empty, restarting loop')
            for step, step_location in enumerate(locations, 1):
                log.debug('Queueing step %d @ %f/%f/%f', step, step_location[0], step_location[1], step_location[2])
                threadStatus['Overseer']['message'] = "Queuing next step"
                search_args = (step, step_location)
                search_items_queue.put(search_args)
        else:
            #   log.info('Search queue processing, %d items left', search_items_queue.qsize())
            threadStatus['Overseer']['message'] = "Processing search queue"

        # Now we just give a little pause here
        time.sleep(1)


def search_overseer_thread_ss(args, new_location_queue, pause_bit, encryption_lib_path, db_updates_queue, wh_queue):
    log.info('Search ss overseer starting')
    search_items_queues = []
    parse_lock = Lock()
    spawns = []
    threadStatus = {}

    threadStatus['Overseer'] = {}
    threadStatus['Overseer']['message'] = "Initializing"
    threadStatus['Overseer']['type'] = "Overseer"
    threadStatus['Overseer']['method'] = "Spawn Scan"

    if(args.print_status):
        log.info('Starting status printer thread')
        t = Thread(target=status_printer,
                   name='status_printer',
                   args=(threadStatus, search_items_queues, db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    # Create a search_worker_thread_ss per account
    log.info('Starting search worker threads')
    for i, account in enumerate(args.accounts):
        log.debug('Starting search worker thread %d for user %s', i, account['username'])
        search_items_queues.append(Queue())
        threadStatus['Worker {:03}'.format(i)] = {}
        threadStatus['Worker {:03}'.format(i)]['type'] = "Worker"
        threadStatus['Worker {:03}'.format(i)]['message'] = "Creating thread..."
        threadStatus['Worker {:03}'.format(i)]['success'] = 0
        threadStatus['Worker {:03}'.format(i)]['fail'] = 0
        threadStatus['Worker {:03}'.format(i)]['skip'] = 0
        threadStatus['Worker {:03}'.format(i)]['noitems'] = 0
        t = Thread(target=search_worker_thread_ss,
                   name='ss-worker-{}'.format(i),
                   args=(args, account, search_items_queues[i], parse_lock,
                         encryption_lib_path, threadStatus['Worker {:03}'.format(i)],
                         db_updates_queue, wh_queue))
        t.daemon = True
        t.start()

    if os.path.isfile(args.spawnpoint_scanning):  # if the spawns file exists use it
        threadStatus['Overseer']['message'] = "Getting spawnpoints from file"
        try:
            with open(args.spawnpoint_scanning) as file:
                try:
                    spawns = json.load(file)
                except ValueError:
                    log.error(args.spawnpoint_scanning + " is not valid")
                    return
                file.close()
        except IOError:
            log.error("Error opening " + args.spawnpoint_scanning)
            return
    else:  # if spawns file dose not exist use the db
        threadStatus['Overseer']['message'] = "Getting spawnpoints from database"
        loc = new_location_queue.get()
        spawns = Pokemon.get_spawnpoints_in_hex(loc, args.step_limit)
    spawns = assign_spawns(spawns, len(args.accounts), args.scan_delay, args.max_speed, args.max_delay)
    log.info('Total of %d spawns to track', len(spawns))
    # find the inital location (spawn thats 60sec old)
    pos = SbSearch(spawns, (curSec() + 3540) % 3600)
    while True:
        while timeDif(curSec(), spawns[pos]['time']) < 60:
            threadStatus['Overseer']['message'] = "Waiting for spawnpoints {} of {} to spawn at {}".format(pos, len(spawns), spawns[pos]['time'])
            time.sleep(1)
        # make location with a dummy height (seems to be more reliable than 0 height)
        threadStatus['Overseer']['message'] = "Queuing spawnpoint {} of {}".format(pos, len(spawns))
        location = [spawns[pos]['lat'], spawns[pos]['lng'], 40.32]
        search_args = (pos, location, spawns[pos]['time'])
        search_items_queues[spawns[pos]['worker']].put(search_args)
        pos = (pos + 1) % len(spawns)


def assign_spawns(spawns, num_workers, scan_delay, max_speed, max_delay):

    log.info('Attemping to assign %d spawn points to %d accounts' % (len(spawns), num_workers))

    def dist(sp1, sp2):
        dist = geopy.distance.distance((sp1['lat'], sp1['lng']), (sp2['lat'], sp2['lng'])).meters
        return dist

    def speed(sp1, sp2):
        time = max((sp2['time'] - sp1['time']) % 3600, scan_delay)
        if time == 0:
            return float('inf')
        else:
            return dist(sp1, sp2) / time

    def print_speed(q):
        s = []
        for i in range(len(q)):
            j = (i + 1) % len(q)
            s.append(speed(q[i], q[j]))
        print 'Max speed: %f, avg speed %f' % (max(s), sum(s)/len(s))

    # Insert has has two modes of operation.
    # If dry=True, it will simulate inserting sp and return the "cost" of
    # assigning sp to queue. The cost is a tuple (delay, s0, s1, s2). delay is
    # the delay incurred, s1 is the speed that the worker need to travel to
    # scan sp, s2 is the speed that the worker need to travel after scanning
    # sp, and s0 = max(s1, s2)
    # If dry=False, it will insert sp to the proper location
    def insert(queue, sp, dry):
        # make a copy so we don't change the spawnpoint passed in
        sp = dict(sp)

        if len(queue) == 0:
            if not dry:
                queue.append(sp)
            return 0, max_speed, max_speed, max_speed

        # Find the slot to insert sp
        l = [True if p['time'] <= sp['time'] else False for p in queue]
        # k is the slot to call `insert' with at the end
        k = l.index(False) if False in l else len(l)
        # i is the previous point index
        i = (k - 1) % len(queue)
        # j is the next point index
        j = k % len(queue)

        # Make scan time at least scan_delay after the previous point
        sp['time'] = max(sp['time'], queue[i]['time'] + scan_delay)

        # Calculate scanner speeds incurred by adding sp
        s1 = speed(queue[i], sp)
        s2 = speed(sp, queue[j])

        if i != j and (queue[j]['time'] - queue[i]['time']) % 3600 < 2 * scan_delay:
            # No room for sp
            score = (float('inf'), 0, 0, 0)
        elif s1 <= max_speed and s2 <= max_speed:
            # We are all good to go
            score = (0, max(s1, s2), s1, s2)
        elif s2 > max_speed:
            # No room for sp
            score = (float('inf'), 0, 0, 0)
        else:
            # s1 > max_speed, try to wiggle the scan time for sp
            time2wait = (dist(queue[i], sp) / max_speed) - (sp['time'] - queue[i]['time'])
            if time2wait > (sp['time'] - queue[j]['time']) % 3600:
                # Wiggle failed
                score = (float('inf'), 0, 0, 0)
            else:
                # Wiggle successful, add time2wait as delay
                sp['time'] += time2wait
                s1 = max_speed
                s2 = speed(sp, queue[j])
                score = (time2wait, max(s1, s2), s1, s2)

        if not dry and score[0] < float('inf'):
            queue.insert(k, sp)

        return score

    # Tries to assign spawns to n workers, returns the set of delays and the
    # set of points that cannot be covered (bad).
    def greedy_assign(spawns, n):
        spawns.sort(key=itemgetter('time'))
        Q = [[] for i in range(n)]
        delays = []
        bad = []
        for sp in spawns:
            scores = [(insert(q, sp, True), i) for i, q in enumerate(Q)]
            min_score, min_index = min(scores)
            delay, s0, s1, s2 = min_score
            if delay <= max_delay:
                insert(Q[min_index], sp, False)
                if delay > 0:
                    delays.append(delay)
            else:
                bad.append(sp)

        log.info("Assigned %d spawn points to %d workers, left out %d points" %
                (len(spawns) - len(bad), n, len(bad)))
        return Q, delays, bad

    Q, delays, bad = greedy_assign(spawns, num_workers)

    if len(bad):
        log.info('Cannot schedule %d spawnpoints under max_delay, dropping.' % len(bad))

    log.debug('Completed job assignment.')
    log.info('Job queue sizes: %s' % str([len(q) for q in Q]))
    if len(delays):
        log.info('Number of scan delays: %d.' % len(delays))
        log.info('Average delay: %f seconds.' % (sum(delays) / len(delays)))
        log.info('Max delay: %f seconds.' % max(delays))
        if max(delays) > 60:
            log.info('Cannot assign spawn points with delay less than a minute. You should try increasing number of accounts or decreasing number of spawn points.')
    else:
        log.info('No additional delay is added to any spawn point.')

    # Assign worker id to each spown point
    for index, queue in enumerate(Q):
        for sp in queue:
            sp['worker'] = index

    # Merge individual job queues back to one queue and sort it
    spawns = sum(Q, [])
    spawns.sort(key=itemgetter('time'))

    return spawns


def search_worker_thread(args, account, search_items_queue, parse_lock, encryption_lib_path, status, dbq, whq):

    stagger_thread(args, account)

    log.debug('Search worker thread starting')

    # The forever loop for the thread
    while True:
        try:
            log.debug('Entering search loop')
            status['message'] = "Entering search loop"

            # Create the API instance this will use
            if args.mock != '':
                api = FakePogoApi(args.mock)
            else:
                api = PGoApi()

            if args.proxy:
                api.set_proxy({'http': args.proxy, 'https': args.proxy})

            api.activate_signature(encryption_lib_path)

            # Get current time
            loop_start_time = int(round(time.time() * 1000))

            # The forever loop for the searches
            while True:

                # Grab the next thing to search (when available)
                status['message'] = "Waiting for item from queue"
                step, step_location = search_items_queue.get()
                status['message'] = "Searching at {},{}".format(step_location[0], step_location[1])
                log.info('Search step %d beginning (queue size is %d)', step, search_items_queue.qsize())

                # Let the api know where we intend to be for this loop
                api.set_position(*step_location)

                # The loop to try very hard to scan this step
                failed_total = 0
                while True:

                    # After so many attempts, let's get out of here
                    if failed_total >= args.scan_retries:
                        # I am choosing to NOT place this item back in the queue
                        # otherwise we could get a "bad scan" area and be stuck
                        # on this overall loop forever. Better to lose one cell
                        # than have the scanner, essentially, halt.
                        log.error('Search step %d went over max scan_retires; abandoning', step)
                        status['message'] = "Search step went over max scan_retries; abandoning"

                        # Didn't succeed, but we are done with this queue item
                        search_items_queue.task_done()

                        # Sleep for 2 hours, print a log message every 5 minutes.
                        long_sleep_time = 0
                        long_sleep_started = time.strftime("%H:%M")
                        while long_sleep_time < (2 * 60 * 20):
                            log.error('Worker %s failed, possibly banned account. Started 2 hour sleep at %s', account['username'], long_sleep_started)
                            status['message'] = 'Worker {} failed, possibly banned account. Started 2 hour sleep at {}'.format(account['username'], long_sleep_started)
                            long_sleep_time += 300
                            time.sleep(300)
                        break

                    # Increase sleep delay between each failed scan
                    # By default scan_dela=5, scan_retries=5 so
                    # We'd see timeouts of 5, 10, 15, 20, 25
                    sleep_time = args.scan_delay * (1 + failed_total)

                    # Ok, let's get started -- check our login status
                    check_login(args, account, api, step_location)

                    # Make the actual request (finally!)
                    response_dict = map_request(api, step_location, args.jitter)

                    # G'damnit, nothing back. Mark it up, sleep, carry on
                    if not response_dict:
                        log.error('Search step %d area download failed, retrying request in %g seconds', step, sleep_time)
                        failed_total += 1
                        status['fail'] += 1
                        status['message'] = "Failed {} times to scan {},{} - no response - sleeping {} seconds. Username: {}".format(failed_total, step_location[0], step_location[1], sleep_time, account['username'])
                        time.sleep(sleep_time)
                        continue

                    # Got the response, parse it out, send todo's to db/wh queues
                    try:
                        findCount = parse_map(args, response_dict, step_location, dbq, whq)
                        log.debug('Search step %s completed', step)
                        search_items_queue.task_done()
                        if findCount > 0:
                            status['success'] += 1
                        else:
                            status['noitems'] += 1
                        break  # All done, get out of the request-retry loop
                    except KeyError:
                        log.exception('Search step %s map parsing failed, retrying request in %g seconds. Username: %s', step, sleep_time, account['username'])
                        failed_total += 1
                        status['fail'] += 1
                        status['message'] = "Failed {} times to scan {},{} - map parsing failed - sleeping {} seconds. Username: {}".format(failed_total, step_location[0], step_location[1], sleep_time, account['username'])
                        time.sleep(sleep_time)

                # If there's any time left between the start time and the time when we should be kicking off the next
                # loop, hang out until its up.
                sleep_delay_remaining = loop_start_time + (args.scan_delay * 1000) - int(round(time.time() * 1000))
                if sleep_delay_remaining > 0:
                    status['message'] = "Waiting {} seconds for scan delay".format(sleep_delay_remaining / 1000)
                    time.sleep(sleep_delay_remaining / 1000)

                loop_start_time += args.scan_delay * 1000

        # catch any process exceptions, log them, and continue the thread
        except Exception as e:
            status['message'] = "Exception in search_worker. Username: {}".format(account['username'])
            log.exception('Exception in search_worker: %s. Username: %s', e, account['username'])
            time.sleep(sleep_time)


def search_worker_thread_ss(args, account, search_items_queue, parse_lock, encryption_lib_path, status, dbq, whq):
    stagger_thread(args, account)
    log.debug('Search worker ss thread starting')
    status['message'] = "Search worker ss thread starting"
    # forever loop (for catching when the other forever loop fails)
    while True:
        try:
            log.debug('Entering search loop')
            status['message'] = "Entering search loop"
            # create api instance
            if args.mock != '':
                api = FakePogoApi(args.mock)
            else:
                api = PGoApi()
            if args.proxy:
                api.set_proxy({'http': args.proxy, 'https': args.proxy})
            api.activate_signature(encryption_lib_path)
            # search forever loop
            while True:
                # Grab the next thing to search (when available)
                status['message'] = "Waiting for item from queue"
                step, step_location, spawntime = search_items_queue.get()
                status['message'] = "Searching at {},{}".format(step_location[0], step_location[1])
                log.info('Searching step %d, remaining %d', step, search_items_queue.qsize())
                if timeDif(curSec(), spawntime) < 840:  # if we arnt 14mins too late
                    # set position
                    api.set_position(*step_location)
                    # try scan (with retries)
                    failed_total = 0
                    while True:
                        if failed_total >= args.scan_retries:
                            log.error('Search step %d went over max scan_retires; abandoning', step)
                            # Didn't succeed, but we are done with this queue item
                            search_items_queue.task_done()

                            # Sleep for 2 hours, print a log message every 5 minutes.
                            long_sleep_time = 0
                            long_sleep_started = time.strftime("%H:%M")
                            while long_sleep_time < (2 * 60 * 20):
                                log.error('Worker %s failed, possibly banned account. Started 2 hour sleep at %s', account['username'], long_sleep_started)
                                status['message'] = 'Worker {} failed, possibly banned account. Started 2 hour sleep at {}'.format(account['username'], long_sleep_started)
                                long_sleep_time += 300
                                time.sleep(300)
                            break
                        sleep_time = args.scan_delay * (1 + failed_total)
                        check_login(args, account, api, step_location)
                        # make the map request
                        response_dict = map_request(api, step_location, args.jitter)
                        # check if got anything back
                        if not response_dict:
                            log.error('Search step %d area download failed, retyring request in %g seconds', step, sleep_time)
                            failed_total += 1
                            status['fail'] += 1
                            status['message'] = "Failed {} times to scan {},{} - no response - sleeping {} seconds. Username: {}".format(failed_total, step_location[0], step_location[1], sleep_time, account['username'])
                            time.sleep(sleep_time)
                            continue
                        # got responce try and parse it
                        try:
                            findCount = parse_map(args, response_dict, step_location, dbq, whq)
                            log.debug('Search step %s completed', step)
                            search_items_queue.task_done()
                            if findCount > 0:
                                status['success'] += 1
                            else:
                                status['noitems'] += 1
                            break  # All done, get out of the request-retry loop
                        except KeyError:
                            log.exception('Search step %s map parsing failed, retrying request in %g seconds. Username: %s', step, sleep_time, account['username'])
                            failed_total += 1
                            status['fail'] += 1
                            status['message'] = "Failed {} times to scan {},{} - map parsing failed - sleeping {} seconds. Username: {}".format(failed_total, step_location[0], step_location[1], sleep_time, account['username'])
                            time.sleep(sleep_time)
                        time.sleep(sleep_time)
                    status['message'] = "Waiting {} seconds for scan delay".format(sleep_time)
                    time.sleep(sleep_time)
                else:
                    search_items_queue.task_done()
                    log.info('Cant keep up. Skipping')
                    status['skip'] += 1
                    status['message'] = "Skipping spawnpoint - can't keep up."
        except Exception as e:
            status['message'] = "Exception in search_worker.  Username: {}".format(account['username'])
            log.exception('Exception in search_worker: %s', e)
            time.sleep(sleep_time)


def check_login(args, account, api, position):

    # Logged in? Enough time left? Cool!
    if api._auth_provider and api._auth_provider._ticket_expire:
        remaining_time = api._auth_provider._ticket_expire / 1000 - time.time()
        if remaining_time > 60:
            log.debug('Credentials remain valid for another %f seconds', remaining_time)
            return

    # Try to login (a few times, but don't get stuck here)
    i = 0
    api.set_position(position[0], position[1], position[2])
    while i < args.login_retries:
        try:
            if args.proxy:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'], proxy_config={'http': args.proxy, 'https': args.proxy})
            else:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'])
            break
        except AuthException:
            if i >= args.login_retries:
                raise TooManyLoginAttempts('Exceeded login attempts')
            else:
                i += 1
                log.error('Failed to login to Pokemon Go with account %s. Trying again in %g seconds', account['username'], args.login_delay)
                time.sleep(args.login_delay)

    log.debug('Login for account %s successful', account['username'])


def map_request(api, position, jitter=False):
    # create scan_location to send to the api based off of position, because tuples aren't mutable
    if jitter:
        # jitter it, just a little bit.
        scan_location = jitterLocation(position)
        log.debug("Jittered to: %f/%f/%f", scan_location[0], scan_location[1], scan_location[2])
    else:
        # Just use the original coordinates
        scan_location = position

    try:
        cell_ids = util.get_cell_ids(scan_location[0], scan_location[1])
        timestamps = [0, ] * len(cell_ids)
        return api.get_map_objects(latitude=f2i(scan_location[0]),
                                   longitude=f2i(scan_location[1]),
                                   since_timestamp_ms=timestamps,
                                   cell_id=cell_ids)
    except Exception as e:
        log.warning('Exception while downloading map: %s', e)
        return False


def stagger_thread(args, account):
    # If we have more than one account, stagger the logins such that they occur evenly over scan_delay
    if len(args.accounts) > 1:
        if len(args.accounts) > args.scan_delay:  # force ~1 second delay between threads if you have many accounts
            delay = args.accounts.index(account) + ((random.random() - .5) / 2) if args.accounts.index(account) > 0 else 0
        else:
            delay = (args.scan_delay / len(args.accounts)) * args.accounts.index(account)
        log.debug('Delaying thread startup for %.2f seconds', delay)
        time.sleep(delay)


class TooManyLoginAttempts(Exception):
    pass
