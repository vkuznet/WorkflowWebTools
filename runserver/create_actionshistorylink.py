#!/usr/bin/env python2.7

"""
.. describe:: create_actionshistorylink.py

A python script that merges information from past actions
with saved historic information into a json file.

:author: Daniel Abercrombie <dabercro@mit.edu>
"""

import sys

if __name__ == '__main__':
    if len(sys.argv) == 1:
        print '\nUsage:  %s <output_file_name>\n' % sys.argv[0]
        exit(1)

    from workflowwebtools import serverconfig
    serverconfig.LOCATION = '.'

    from workflowwebtools import actionshistorylink
    actionshistorylink.dump_json(sys.argv[1])
