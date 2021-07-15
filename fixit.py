#!/usr/bin/env python
# -*- coding: utf-8 -*-
__author__ = 'Fitzeng'
import re
def main():
    fix = open('fix.sh', 'w+')
    for line in open("txt"):
        pkg = re.match(re.compile('''dpkg: warning: files list file for package '(.+)' '''), line)
        if pkg:
            cmd = "sudo apt-get install --reinstall " + pkg.group(1)
            fix.write(cmd + '\n')
if __name__ == "__main__":
    main()
