#pylint: disable=too-many-locals, too-complex, global-statement

"""
Generates the content for the errors pages

:author: Daniel Abercrombie <dabercro@mit.edu>
"""

import os
import sqlite3
import time
import threading

from collections import defaultdict

import cherrypy

from cmstoolbox import sitereadiness

from . import workflowinfo
from . import errorutils
from . import serverconfig
from .reasonsmanip import reasons_list

class ErrorInfo(object):
    """Holds the information for any errors for a session"""

    def __init__(self, data_location=''):
        """Initialization with a setup.
        :param str data_location: Set the location of the data to read in the info
        """

        self.data_location = data_location

        # These are setup by setup()
        self.timestamp = None
        self.conn = None
        self.curs = None
        self.db_lock = threading.Lock()
        # These are setup by set_all_lists(), which is called in setup()
        self.info = None
        self.allsteps = None
        self.readiness = None
        # This is created in clusterworkflows.get_workflow_groups()
        self.clusters = {}
        # These are set in get_workflow()
        self.workflowinfos = {}
        # These are set in get_prepid()
        self.prepidinfos = {}
        # Filled by _get_step_tables
        self._step_tables = None
        # Filled by get_step_list
        self._step_list = None

        self.setup()

    def __del__(self):
        """Delete anything left over."""
        self.teardown()

    def execute(self, query, params=None):
        """
        Locks the internal database and makes the query.

        :param str query: The query, which can include '?'
        :param tuple params: The parameters to pass into the query
        :returns: The results of the query
        :rtype: list
        """

        output = []

        self.db_lock.acquire()
        try:
            if params:
                self.curs.execute(query, params)
            else:
                self.curs.execute(query)

            output = list(self.curs.fetchall())
        finally:
            self.db_lock.release()

        return output


    def setup(self):
        """Create an SQL database from the all_errors.json generated by production"""

        self.timestamp = time.time()

        if self.data_location:
            data_location = self.data_location
        else:
            data_location = serverconfig.all_errors_path()

        # Store everything into an SQL database for fast retrival

        if isinstance(data_location, str) and data_location.endswith('.db') \
                and os.path.exists(data_location):
            self.conn = sqlite3.connect(data_location, check_same_thread=False)
            curs = self.conn.cursor()
            self.curs = curs

        else:
            self.conn = sqlite3.connect(':memory:', check_same_thread=False)
            curs = self.conn.cursor()
            self.curs = curs

            errorutils.create_table(self)
            errorutils.add_to_database(self, data_location)


        self.set_all_lists()
        self.readiness = [sitereadiness.site_readiness(site) for site in self.info[3]]

        if not self.data_location:
            current_workflows = self.return_workflows()

            prep_ids = set([self.get_workflow(wf).get_prep_id() for wf in current_workflows])

            other_workflows = sum([self.get_prepid(prep_id).get_workflows() \
                                       for prep_id in prep_ids], [])

            errorutils.add_to_database(self, [new for new in other_workflows \
                                                  if new not in current_workflows])
            self.set_all_lists()
            current_workflows = self.return_workflows()

            # If all ACDCs are to be shown, include the ones with zero errors like this
            if serverconfig.config_dict().get('include_all_acdcs'):
                self.allsteps.extend(['/%s/' % zero for zero in other_workflows \
                                          if zero not in current_workflows])
                self.allsteps.sort()

            self.readiness = [sitereadiness.site_readiness(site) for site in self.info[3]]

        self.connection_log('opened')

    def set_all_lists(self):
        """
        Get sets the list of all steps, sites, and errors for an ErrorInfo object.
        This should be called if data is added to the ErrorInfo cursor manually.
        """

        def get_all(column):
            """Get list of all unique entries in the database

            :param str column: is the name of the column
            :returns: a list of unique column entries
            :rtype: list
            """

            return [entry[0] for entry in \
                        self.execute('SELECT DISTINCT {0} FROM workflows'.format(column))]

        def safe_int(element):
            """A sorting algorithm that strings don't break.

            :params str element: A string that should be a number,
                                 but is taken care of in the event that it's not.
            :returns: Either the string as an integer or the string unchanged.
            :rtype: int or str
            """
            try:
                return int(element)
            except ValueError:
                return element

        allsteps = get_all('stepname')
        allsteps.sort()
        allsites = get_all('sitename')
        allsites.sort()
        allerrors = get_all('errorcode')
        allerrors.sort(key=safe_int)

        self.info = self, allsteps, allerrors, allsites

        self.allsteps = allsteps

    def teardown(self):
        """Close the database when cache expires"""
        self._step_tables = None
        self._step_list = None

        self.conn.close()
        self.connection_log('closed')

        if self.clusters:
            self.clusters = {}

    def connection_log(self, action):
        """Logs actions on the sqlite3 connection

        :param str action: is the action on the connection
        """
        if cherrypy is not None:  # This happens while everything is being deleted
            cherrypy.log('Connection {0} with timestamp {1}'.format(action, self.timestamp))

    def get_allmap(self):
        """
        :returns: A dictionary that maps 'errorcode', 'stepname', and 'sitename'
                  to the lists of all the errors, steps, or sites
        :rtype: dict
        """

        return {  # lists of elements to call for each possible row and column
            'errorcode': self.info[2],
            'stepname':  self.info[1],
            'sitename':  self.info[3]
            }

    def return_workflows(self):
        """
        :returns: the ordered list of all workflow prep IDs that need attention
        :rtype: list
        """
        wfs = list()

        last = ''

        for step in self.allsteps:
            val = step.split('/')[1]
            if val != last:
                wfs.append(val)
                last = val

        return wfs

    def get_workflow(self, workflow):
        """
        This should be used to get the workflow info so that there is no
        redundant fetching for a single session.

        :param str workflow: The prep ID for a workflow
        :returns: Cached WorkflowInfo from the ToolBox.
        :rtype: WorkflowWebTools.workflowinfo.WorkflowInfo
        """
        if not self.workflowinfos.get(workflow):
            self.workflowinfos[workflow] = workflowinfo.WorkflowInfo(workflow)

        return self.workflowinfos[workflow]

    def get_prepid(self, prep_id):
        """
        :param str prep_id: The name of the Prep ID to check cache for
        :returns: Either cached PrepIDInfo, or a new one
        :rtype: WorkflowWebTools.workflowinfo.PrepIDInfo
        """
        if not self.prepidinfos.get(prep_id):
            self.prepidinfos[prep_id] = workflowinfo.PrepIDInfo(prep_id)

        return self.prepidinfos[prep_id]

    def get_step_list(self, workflow):
        """Gets the list of steps within a workflow

        :param str workflow: Name of the workflow to gather information for
        :returns: list of steps withing the workflow
        :rtype: list
        """

        if self._step_list is None:
            self._step_list = defaultdict(list)
            self.db_lock.acquire()
            self.curs.execute('SELECT DISTINCT(stepname) FROM workflows ORDER BY stepname')
            for tup in self.curs.fetchall():
                stepname = tup[0]
                self._step_list[stepname.split('/')[1]].append(stepname)
            self.db_lock.release()

        return self._step_list[workflow]

    def _get_step_tables(self):
        """Sets the internal step tables for fast fetching"""

        # The keys are stepname, then sitereadiness
        self._step_tables = defaultdict(lambda: defaultdict(list))

        for step, ready, errors, site, code in self.execute(
                """
                SELECT stepname, sitereadiness, numbererrors, sitename, errorcode FROM workflows
                ORDER BY errorcode ASC, sitename ASC
                """):
            # Append everything to 'all' to keep the order
            self._step_tables[step]['all'].append((errors, site, code))
            # Order is not as important when we are getting sparse for different readiness
            self._step_tables[step][ready].append((errors, site, code))

    def get_step_table(self, step, readymatch=None):
        """
        Get the sparse representation of the step table.
        Fetches from an internal dictionary, so faster than database access

        :param str step: The step name for the table
        :param list readymatch: The list of site readiness statuses to match
        :returns: The list used to build the step table.
                  Each element of the list is a tuple of
                  ``(number of errors, site name, exit code)``
        :rtype: list of tuples
        """

        if self._step_tables is None:
            cherrypy.log('Setting up step tables')
            self._get_step_tables()

        keys = readymatch or ['all']

        output = []
        for key in keys:
            output.extend(self._step_tables[step][key])

        return output


GLOBAL_INFO = None
GLOBAL_LOCK = threading.Lock()


def check_session(session, can_refresh=False):
    """If session is None, fills it.

    :param cherrypy.Session session: the current session
    :param bool can_refresh: tells the function if it is safe to refresh
                             and close the old database
    :returns: ErrorInfo of the session
    :rtype: ErrorInfo
    """

    if session:
        GLOBAL_LOCK.acquire()
        if session.get('lock') is None:
            session['lock'] = threading.Lock()
        GLOBAL_LOCK.release()

        session['lock'].acquire()

        if session.get('info') is None:
            session['info'] = ErrorInfo()
        theinfo = session.get('info')

    else:
        GLOBAL_LOCK.acquire()
        global GLOBAL_INFO
        if GLOBAL_INFO is None:
            GLOBAL_INFO = ErrorInfo()

        theinfo = GLOBAL_INFO
        GLOBAL_LOCK.release()

    # If session ErrorInfo is old, set up another connection
    if can_refresh and theinfo.timestamp < time.time() - 60*30:
        theinfo.teardown()
        theinfo.setup()

    if session:
        session['lock'].release()

    return theinfo


def default_errors_format():
    """
    Gives a defaultdict with the format::

      {group1: {'errors': {group1_1: {group1_1_1: errors, group1_1_2: errors}}, group2: ...}}

    :returns: A defaultdict for building errors
    :rtype: collections.defaultdict
    """

    return defaultdict(lambda: {'errors': defaultdict(lambda: defaultdict(lambda: 0)),
                                'sub': {}, 'total': 0})


def group_errors(input_errors, grouping_function, **kwargs):
    """
    Takes inputs errors with the format::

      {group1: {'errors': {group1_1: {group1_1_1: errors, group1_1_2: errors}}, group2: ...}}

    and sums the errors into a larger group.
    This second grouping is done by the output of the grouping_function.

    :param dict input_errors: The input that will be grouped
    :param grouping_function: Takes an input, which is a key of ``input_errors``
                              and groups those keys by this function output.
    :type grouping_function: function
    :param kwargs: The keyword should point to a function.
                   That keyword will be added to the dictionary of each group.
                   It's value will be the function output with the group as an argument.
    :returns: A dictionary with the same format as the input, but with groupings.
    :rtype: defaultdict
    """

    output = default_errors_format()

    for subgroup, values in input_errors.iteritems():

        group = grouping_function(subgroup)

        # We have three variables for everything, so we can write this by hand
        # Not ideal
        for row, row_val in values['errors'].iteritems():
            for col, numerrors in row_val.iteritems():
                output[group]['errors'][row][col] += numerrors

        output[group]['sub'][subgroup] = values
        output[group]['total'] += values['total']

        for key, func in kwargs.iteritems():
            output[group][key] = func(group)

    return output


def get_step_table(step, session=None, allmap=None, readymatch=None,
                   sparse=False):
    """Gathers the errors for a step into a 2-D table of ints

    :param str step: name of the step to get the table for
    :param cherrypy.Session session: Stores the information for a session
    :param dict allmap: a globalerrors.ErrorInfo allmap to override the
                        session's allmap
    :param tuple readymatch: Match the readiness statuses in this tuple, if set
    :param bool sparse: Determines whether or not a sparse matrix is returned
    :returns: A table (made of lists) of errors for the step or a sparse dictionary of entries
    :rtype: list of lists or dict of dicts of ints
    """
    info = check_session(session)
    if not allmap:
        allmap = info.get_allmap()

    contents = info.get_step_table(step, readymatch)

    if sparse:
        output = defaultdict(lambda: defaultdict(lambda: 0))

        for numbererrors, sitename, errorcode in contents:
            output[str(errorcode)][sitename] = numbererrors

        return output

    # If not sparse

    numbererrors, sitename, errorcode = contents.pop(0) if contents else (0, '', '')
    steptable = []

    for error in allmap['errorcode']:

        steprow = []

        for site in allmap['sitename']:

            if error != errorcode or site != sitename:
                steprow.append(0)
            else:
                steprow.append(numbererrors)
                numbererrors, sitename, errorcode = contents.pop(0) if contents else (0, '', '')

        steptable.append(steprow)

    return steptable


def see_workflow(workflow, session=None):
    """Gathers the error information for a single workflow

    :param str workflow: Name of the workflow to gather information for
    :param cherrypy.Session session: Stores the information for a session
    :returns: Dictionary used to generate webpage for a requested workflow
    :rtype: dict
    """

    _, _, allerrors, allsites = check_session(session).info
    steplist = check_session(session).get_step_list(workflow)

    tables = []
    # Each key is a step, and contains a list of sites to not put in the table
    skip_site = {}

    for step in steplist:
        skip_site[step] = {'sites': [], 'index': []}
        steptable = get_step_table(step, session)
        tables.append(zip(steptable, allerrors))
        for index, site in enumerate(allsites):
            if sum([row[index] for row in steptable]) == 0:
                skip_site[step]['index'].append(index)
                skip_site[step]['sites'].append(site)

    return {
        'steplist':  zip(steplist, tables),
        'allerrors': allerrors,
        'allsites':  allsites,
        'skips': skip_site,
        'reasonslist': reasons_list(),
        }


def get_row_col_names(pievar):
    """Get the column and row for the global table view, based on user input

    :param str pievar: The variable to divide the piecharts by.
    :returns: The names of the global table rows, and the table columns
    :rtype: (str, str)
    """

    pievarmap = { # for each pievar, set row and column
        'errorcode': ('stepname', 'sitename'),
        'sitename':  ('stepname', 'errorcode'),
        'stepname':  ('errorcode', 'sitename')
        }

    # Check for valid pievar and set default
    return pievarmap[pievar if pievar in pievarmap.keys() else 'errorcode']


TITLEMAP = {
    'errorcode': 'error code',
    'stepname':  'workflow',
    'sitename':  'site name',
    }
"""Dictionary that determines how a chosen pievar shows up in the pie chart titles"""


def list_matching_pievars(pievar, row, col, session=None):
    """
    Return an iterator of variables in pievar, and number of errors
    for a given rowname and colname

    :param str pievar: The variable to return an iterator of
    :param str row: Name of the row to match
    :param str col: Name of the column to match
    :param cherrypy.Session session: stores the session information
    :returns: List of tuples containing name of pievar and number of errors
    :rtype: list
    """

    curs = check_session(session, can_refresh=True)
    rowname, colname = get_row_col_names(pievar)

    output = []

    # Let's do this very carefully and stupid for now...
    for name, num in  curs.execute(('SELECT {0}, numbererrors FROM workflows '
                                    'WHERE {1}=? AND {2}=?'.
                                    format(pievar, rowname, colname)),
                                   (row, col)):
        output.append((name, num))

    return output


def get_errors(pievar, session=None):
    """
    Gets the number of errors with the format::

      {group1: {'errors': {group1_1: {group1_1_1: errors, group1_1_2: errors}}, group2: ...}}

    where each group is a different value for the variables that
    go into the row of the global errors table.
    That is, the groups will usually be the subtask list, unless ``pievar`` is ``"stepname"``.
    In that case, the grouping is by error code.

    :param str pievar: The variable that each piechart is split into.
    :param cherrypy.Session session: Stores the information for a session
    :returns: A dictionary of 2D list of errors.
    :rtype: defaultdict
    """

    rowname, colname = get_row_col_names(pievar)

    query = 'SELECT numbererrors, {0}, {1}, {2} FROM workflows ' \
        'ORDER BY {0} ASC, {1} ASC, {2} ASC;'.format(rowname, colname, pievar)

    curs = check_session(session, True)
    contents = curs.execute(query)

    output = default_errors_format()

    for numerrors, row, col, pvar in contents:
        output[row]['errors'][col][pvar] = numerrors
        output[row]['total'] += numerrors

    return output
