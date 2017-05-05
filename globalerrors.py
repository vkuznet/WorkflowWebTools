#pylint: disable=too-many-locals

"""
Generates the content for the errors pages

:author: Daniel Abercrombie <dabercro@mit.edu>
"""

import os
import sqlite3
import time
import validators
import cherrypy
import numpy

from CMSToolBox import sitereadiness
from CMSToolBox import workflowinfo

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
        self.curs = None
        self.conn = None
        # These are setup by set_all_lists(), which is called in setup()
        self.info = None
        self.allsteps = None
        self.readiness = None
        # This is created in clusterworkflows.get_workflow_groups()
        self.clusters = None
        # These are set in get_workflow()
        self.workflowinfos = {}

        self.setup()

    def __del__(self):
        """Delete anything left over."""
        self.teardown()

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

        else:
            self.conn = sqlite3.connect(':memory:', check_same_thread=False)
            curs = self.conn.cursor()

            errorutils.create_table(curs)
            errorutils.add_to_database(curs, data_location)


        self.curs = curs
        self.set_all_lists()
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

            self.curs.execute('SELECT DISTINCT {0} FROM workflows'.format(column))
            return [entry[0] for entry in self.curs.fetchall()]

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

        data_location = serverconfig.explain_errors_path()

        if not (os.path.isfile(data_location) or validators.url(data_location)):
            self.info = self.curs, allsteps, allerrors, allsites, {}

        else:
            self.info = self.curs, allsteps, allerrors, allsites, \
                errorutils.open_location(data_location)

        self.allsteps = allsteps

    def teardown(self):
        """Close the database when cache expires"""
        self.conn.close()
        self.connection_log('closed')

        if self.clusters:
            self.clusters['conn'].close()
            self.clusters = None

    def connection_log(self, action):
        """Logs actions on the sqlite3 connection

        :param str action: is the action on the connection
        """
        cherrypy.log('Connection {0} with timestamp {1}'.format(action, self.timestamp))

    def get_errors_explained(self):
        """
        :returns: Dictionary that maps each error code to a snippet of the error log
        :rtype: dict
        """
        return self.info[4]

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
        :rtype: CMSToolBox.workflowinfo.WorkflowInfo
        """
        if not self.workflowinfos.get(workflow):
            self.workflowinfos[workflow] = workflowinfo.WorkflowInfo(workflow)

        return self.workflowinfos[workflow]

    def get_step_list(self, workflow):
        """Gets the list of steps within a workflow

        :param str workflow: Name of the workflow to gather information for
        :returns: list of steps withing the workflow
        :rtype: list
        """

        steplist = list(     # Make a list of all the steps so we can sort them
            set(
                [stepgets[0] for stepgets in self.curs.execute(
                    "SELECT stepname FROM workflows WHERE stepname LIKE '/{0}/%'".format(workflow)
                    )
                ]
                )
            )
        steplist.sort()

        return steplist


GLOBAL_INFO = ErrorInfo()


def check_session(session, can_refresh=False):
    """If session is None, fills it.

    :param cherrypy.Session session: the current session
    :param bool can_refresh: tells the function if it is safe to refresh
                             and close the old database
    :returns: ErrorInfo of the session
    :rtype: ErrorInfo
    """

    if session:
        if session.get('info') is None:
            session['info'] = ErrorInfo()
        theinfo = session.get('info')
    else:
        theinfo = GLOBAL_INFO

    # If session ErrorInfo is old, set up another connection
    if can_refresh and theinfo.timestamp < time.time() - 60*30:
        theinfo.teardown()
        theinfo.setup()

    return theinfo


def group_errors(input_errors, grouping_function):
    """
    Takes inputs errors with the format::

      {group1: {'errors': [[]], 'sub': {}}, group2: ...}

    and sums the errors into a larger group.
    This second grouping is done by the output of the grouping_function.

    :param dict input_errors: The input that will be grouped
    :param grouping_function: Takes an input, which is a key of ``input_errors``
                              and groups those keys by this function output.
    :type grouping_function: function
    :returns: A dictionary with the same format as the input, but with groupings.
    :rtype: dict
    """

    output = {}

    for subgroup, values in input_errors.iteritems():

        group = grouping_function(subgroup)

        if group in output.keys():
            output[group]['errors'] += numpy.matrix(values['errors'])
            output[group]['sub'][subgroup] = values
        else:
            output[group] = {
                'errors': numpy.matrix(values['errors']),
                'sub': {
                    subgroup: values
                    }
                }

    for group in output:
        output[group]['errors'] = output[group]['errors'].tolist()

    return output


def get_step_table(step, session=None, allmap=None, readymatch=None):
    """Gathers the errors for a step into a 2-D table of ints

    :param str step: name of the step to get the table for
    :param cherrypy.Session session: Stores the information for a session
    :param dict allmap: a globalerrors.ErrorInfo allmap to override the
                        session's allmap
    :param tuple readymatch: Match the readiness statuses in this tuple, if set
    :returns: A table of errors for the step
    :rtype: list of lists of ints
    """
    curs = check_session(session).curs
    if not allmap:
        allmap = check_session(session).get_allmap()

    steptable = []

    query = 'SELECT numbererrors, sitename, errorcode FROM workflows ' \
        'WHERE stepname=?'
    params = (step,)
    if readymatch:
        query += ' AND ({0})'.format(' OR '.join(['sitereadiness=?']*len(readymatch)))
        params += readymatch

    query += ' ORDER BY errorcode ASC, sitename ASC'
    curs.execute(query, params)

    numbererrors, sitename, errorcode = curs.fetchone() or (0, '', '')

    for error in allmap['errorcode']:

        steprow = []

        for site in allmap['sitename']:

            if error != errorcode or site != sitename:
                steprow.append(0)
            else:
                steprow.append(numbererrors)
                numbererrors, sitename, errorcode = curs.fetchone() or (0, '', '')

        steptable.append(steprow)

    return steptable


def see_workflow(workflow, session=None):
    """Gathers the error information for a single workflow

    :param str workflow: Name of the workflow to gather information for
    :param cherrypy.Session session: Stores the information for a session
    :returns: Dictionary used to generate webpage for a requested workflow
    :rtype: dict
    """

    _, _, allerrors, allsites, _ = check_session(session).info
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
    if pievar not in pievarmap.keys():
        pievar = 'errorcode'

    return pievarmap[pievar]


TITLEMAP = {
    'errorcode': 'code ',
    'stepname':  'step ',
    'sitename':  'site ',
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

    curs = check_session(session, True).curs
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

      {group1: {'errors': [[]]}, group2: ...}

    where each group is a different value for the variables that
    go into the row of the global errors table.
    That is, the groups will usually be the subtask list, unless ``pievar`` is ``"stepname"``.
    In that case, the grouping is by error code.

    :param str pievar: The variable that each piechart is split into.
    :param cherrypy.Session session: Stores the information for a session
    :returns: A dictionary of 2D list of errors.
    :rtype: dict
    """

    rowname, colname = get_row_col_names(pievar)
    allmap = check_session(session).get_allmap()

    query = 'SELECT numbererrors, {0}, {1}, {2} FROM workflows ' \
        'ORDER BY {0} ASC, {1} ASC, {2} ASC;'.format(rowname, colname, pievar)

    curs = check_session(session).curs
    curs.execute(query)

    numerrors, this_row, this_col, this_pievar = curs.fetchone() or (0, '', '', '')

    output = {}
    total_pie_vars = [0] * len(allmap[pievar])

    for row in allmap[rowname]:
        output[row] = {
            'errors': [[0] * len(allmap[pievar]) for _ in allmap[colname]]
            }
        for icol, col in enumerate(allmap[colname]):
            for ipie, pie in enumerate(allmap[pievar]):
                if (row, col, pie) == (this_row, this_col, this_pievar):
                    output[row]['errors'][icol][ipie] = numerrors
                    total_pie_vars[ipie] += numerrors
                    numerrors, this_row, this_col, this_pievar = \
                        curs.fetchone() or (0, '', '', '')

    # Sort the pievars
    indices_for_pievars = [index for index, _ in sorted(
        enumerate(total_pie_vars), key=(lambda x: x[1]), reverse=True)]

    for key, errors in output.iteritems():
        for icol, col in enumerate(errors['errors']):
            output[key]['errors'][icol] = [col[index] for index in indices_for_pievars]

    return output
