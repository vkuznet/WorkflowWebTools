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
        :returns: the set of all workflow prep IDs that need attention
        :rtype: set
        """
        wfs = set()

        for step in self.allsteps:
            wfs.add(step.split('/')[1])

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

    numbererrors, sitename, errorcode = (0, '', '')
    fetch = True

    for error in allmap['errorcode']:

        steprow = []

        for site in allmap['sitename']:

            if fetch:
                line = curs.fetchone()
                if line:
                    numbererrors, sitename, errorcode = line
                fetch = False

            if error != errorcode or site != sitename:
                steprow.append(0)
            else:
                steprow.append(numbererrors)
                fetch = True

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


def get_errors_and_pietitles(pievar, session=None):
    """Gets the number of errors for the global table.

    :ref:`piechart-ref` contains the function that actually draws the piecharts.

    :param str pievar: The variable to divide the piecharts by.
                       This is the variable that does not make up the axes of the page table
    :param cherrypy.Session session: Stores the information for a session
    :returns: Errors for global table and titles for each pie chart.
              The errors are split into two variables.

               - The first variable is a dictionary with two keys: 'col' and 'row'.
                 Each item is a list of the total number of errors in each column or row.
               - The second variable is just a long list lists of ints.
                 One element of the first layer corresponds to
                 a pie chart on the globalerrors view.
                 Each element of the second layer tells different slices of the pie chart.
                 This is read in by the javascript.
               - The last variable is the list of titles to give each pie chart.
                 This will show up in a tooltip on the webpage.
    :rtype: dict, list of lists, list
    """


    rowname, colname = get_row_col_names(pievar)

    allmap = check_session(session).get_allmap()

    pieinfo = []
    pietitles = []

    total_errors = {
        'row': [0] * len(allmap[rowname]),
        'col': [0] * len(allmap[colname])
        }

    for irow, row in enumerate(allmap[rowname]):

        pietitlerow = []

        for icol, col in enumerate(allmap[colname]):
            toappend = []
            piemap = {}
            pietitle = ''
            if rowname != 'stepname':
                pietitle += TITLEMAP[rowname] + ': ' + str(row) + '\n'
            pietitle += TITLEMAP[colname] + ': ' + str(col)
            for piekey, errnum in list_matching_pievars(pievar, row, col, session):

                piemap[piekey] = errnum

                if errnum != 0:
                    toappend.append(errnum)
                    pietitle += '\n' + TITLEMAP[pievar] + str(piekey) + ': ' + str(errnum)

            sum_errors = sum(toappend)
            pietitlerow.append('Total Errors: ' + str(sum_errors) + '\n' + pietitle)

            total_errors['row'][irow] += sum_errors
            total_errors['col'][icol] += sum_errors

            # Append all the pie info for every possibility
            pieinfo.append([piemap.get(value, 0) for value in allmap[pievar]])

        pietitles.append(pietitlerow)

    # Sort the pieinfo so that the maximum contributor is red

    sum_list = [0] * len(allmap[pievar])

    for cell in pieinfo:
        sum_list = [value + cell[index] for index, value in enumerate(sum_list)]

    sorted_pieinfo = []

    for info in pieinfo:
        sorted_pieinfo.append([info[index] for index, _ in sorted(
            enumerate(sum_list), key=(lambda x: x[1]), reverse=True)])

    return total_errors, sorted_pieinfo, pietitles


def get_header_titles(varname, errors, session=None):
    """Gets the titles that will end up being the <th> tooltips for the global view

    :param str varname: Name of the column or row variable
    :param list errors: A list of the total number of errors for the row or column
    :param cherrypy.Session session: Stores the information for a session
    :returns: A list of strings of the titles based on the column or row variable
              and the number of errors
    :rtype: list
    """

    output = []

    for name in check_session(session).get_allmap()[varname]:

        if varname == 'stepname':
            newnamelist = name.lstrip('/').split('/')
            newname = newnamelist[0] + '<br>' + '/'.join(newnamelist[1:])
            output.append({'title': name, 'name': newname})

        else:
            output.append({'title': name, 'name': name})

    for i, title in enumerate(output):
        title['title'] = ('Total errors: ' + str(errors[i]) + '\n' +
                          str(title['title']))

        if varname == 'errorcode':
            title['name'] = '<a href="/explainerror?errorcode={0}">{0}</a>'.format(title['name'])

    return output


def return_page(pievar, session=None):
    """Get the information for the global views webpage

    :param str pievar: The variable to divide the piecharts by.
                       This is the variable that does not make up the axes of the page table
    :param cherrypy.Session session: Stores the information for a session
    :returns: Dictionary of information used by the global views page
              Of interest for other applications is the dictionary member 'steplist'.
              It is a tuple of the names of each step and the errors table for each of these steps.
              The table is an array of rows of the number of errors. Each row is an error code,
              and each column is a site.
    :rtype: dict
    """

    # Based on the dimesions from the user, create a list of pies to show
    rowname, colname = get_row_col_names(pievar)

    total_errors, pieinfo, pietitles = get_errors_and_pietitles(pievar, session)

    return {
        'collist': get_header_titles(colname, total_errors['col'], session),
        'pieinfo': pieinfo,
        'rowzip':  zip(get_header_titles(rowname, total_errors['row'], session), pietitles),
        'pievar':  pievar,
        }
