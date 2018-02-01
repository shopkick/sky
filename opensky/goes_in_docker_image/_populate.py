
import os
import os.path
import sys
import time
import json
import socket
import subprocess

# import gflags
import cloudfiles
import sqlalchemy as sa
import sqlalchemy.orm

# from lib.logging.analytics.datawarehouse.arch2.prod_cleanup import cleanup_versioned_tables
# from tools.pseudo_localization.pseudo_localization import pseudo_localize_file

# flags disabled until proven necessary
# gflags.DEFINE_bool("pseudo_localize", False,
#                    "Should the content be pseudo localized(english -> unicode")
#gflags.DEFINE_bool("populate_should_update_zookeeper_nodes", True,
#                   "Should running populate update zookeeper nodes for versioned tables?")

# FLAGS = gflags.FLAGS


def quote_string(string):
    # this wraps a string in double quotes, slash escaping any quotes inside
    return json.dumps(string)


""" # These functions included in by service_manager.py
def wait_for(host, port, timeout):
    print 'waiting for', host, port, 'to come ready'
    start = time.time()
    while (time.time() - start) < timeout:
        try:
            socket.create_connection( (host, port) )
            break
        except socket.error:
            print '  - still waiting for', host, port, 'to come ready'
            time.sleep(0.3)
    else:
        print 'TIMED OUT AFTER', timeout, 'SECONDS WAITING FOR', host, port
        sys.exit(1)


def run_cmd(*argv):
    print 'running command', ' '.join(argv)
    return subprocess.check_call(argv, env=ENV, cwd='/home/app/{project_name}')
"""

def run_root_query(query, reraise=True):
    try:
        return run_cmd('mysql', '--host=mysql', '--port=3306', '-u', 'root', '-e', query)
    except subprocess.CalledProcessError:
        if reraise:
            raise
        print 'query failed: %r' % query
        return False
    return True


def populate():
    wait_for('mysql', 3306, 10)
    
    engine = sa.create_engine("mysql://root@mysql")
    session_maker = sqlalchemy.orm.sessionmaker(bind=engine, expire_on_commit=True)
    session = session_maker()
    r = engine.execute("show global variables like 'innodb_buffer_pool_size'")
    buffer_pool_size = int(r.fetchone()[1])
    if buffer_pool_size < 32 * 1024*1024:
      print "******ERROR******"
      print "Your buffer pool size is very small, this will make this populate slower."
      print "To create a larger buffer pool:"
      print "1. Edit /etc/my.cnf and make it look something like this:"
      print ""
      print "[mysqld]"
      print "innodb_buffer_pool_size=64M"
      print ""
      print "2. Restart Mysql. You can usually do this on Macs by doing:"
      print "\t sudo /Library/StartupItems/MySQLCOM/MySQLCOM restart"
      sys.exit(1)
    """
    # not clear why this stuff exists. maybe for testing.
    if FLAGS.pseudo_localize:
      r = engine.execute("show global variables like 'max_allowed_packet'")
      max_allowed_packet = int(r.fetchone()[1])
      if max_allowed_packet < 32 * 1024*1024:
        print "******ERROR******"
        print "Your buffer pool size is very small, this will make this populate slower."
        print "To create a larger buffer pool:"
        print "1. Edit /etc/my.cnf and make it look something like this:"
        print ""
        print "[mysqld]"
        print "max_allowed_packet = 64M"
        print ""
        print "2. Restart Mysql. You can usually do this on Macs by doing:"
        print "\t sudo /Library/StartupItems/MySQLCOM/MySQLCOM restart"
        sys.exit(1)
    """

    # Download the backup
    tgz_save_file_path = "/tmp/dev-tables.tgz"
    dev_tables_path = "/tmp/dev-tables-dir"

    # TODO (steven): Get USER / API_KEY securely
    USER = "mobshop"
    API_KEY = "deddb860790712f6e1392e7dc09b5404"

    print "Downloading last dump"
    conn = cloudfiles.get_connection(USER, API_KEY)
    container = conn.get_container("prod-db-dev-tables")
    obj = container.get_object("dev-tables.tgz")
    obj.save_to_filename(tgz_save_file_path)
    
    try:
      os.makedirs(dev_tables_path)
    except OSError:
      pass

    print "Remove old dumps"
    for fn in os.listdir(dev_tables_path):
      fullpath = os.path.join(dev_tables_path, fn)
      os.remove(fullpath)

    print "Extracting the dump"
    run_cmd("tar", "xzvf", tgz_save_file_path, "-C", dev_tables_path)

    with open("/tmp/setup.sql", "w") as outfile:
      outfile.write("SET sql_log_bin=0; SET unique_checks=0; SET foreign_key_checks=0;")

    print "Beginning Load"
    sql_files = sorted(os.listdir(dev_tables_path))

    db_names = sorted(set([s.partition('.')[0] for s in sql_files]))
    for i, db in enumerate(db_names):
        print "\tCreating database %s (%s/%s)" % (db, i + 1, len(db_names))
        run_root_query('CREATE DATABASE IF NOT EXISTS %s' % db)
    
    for i, fn in enumerate(sql_files):
      print "\tLoading %s (table %s/%s)" % (fn, i + 1, len(sql_files))
      db_name = fn.partition(".")[0]
      fullpath = os.path.join(dev_tables_path, fn)
      # if FLAGS.pseudo_localize:
      #   pseudo_localize_file(fullpath)
      # TODO: improve
      run_cmd('bash', '-c', 'cat /tmp/setup.sql %s | mysql --host=mysql --port=3306 -u root %s' % (fullpath, db_name))

    # Populate location_index
    print "Truncating old location index"
    run_root_query('truncate table location.location_index', reraise=False)
    
    print "Rebuilding location index"
    run_root_query(
        'INSERT into location.location_index (select id, Point(latitude, longitude) from location.location)', reraise=False)
    
    print "Truncating old chain_location index"
    run_root_query(
            'truncate table location.chain_location_index', reraise=False)
    
    print "Rebuilding chain_location index"
    run_root_query('INSERT into location.chain_location_index (select id, chain_id,'
                   ' Point(latitude, longitude) from location.location where chain_id is not NULL AND hidden=0'
                   ' AND (miss_count <= 30 OR miss_count IS NULL))', reraise=False)
    
    print "Rebuilding transmitter_location_index"
    run_root_query('truncate table location.transmitter_location_index', reraise=False)
    run_root_query('INSERT INTO location.transmitter_location_index'
                   ' (SELECT id, chain_id, Point(latitude, longitude) FROM location.location '
                   ' WHERE chain_id IS NOT NULL AND hidden=0 AND (miss_count <= 30 OR miss_count IS NULL)'
                   ' AND id IN (SELECT location_id FROM location.ultrasonic_transmitters))', reraise=False)
    
    print "Rebuilding btle_transmitter_location_index"
    run_root_query('truncate table location.btle_transmitter_location_index', reraise=False)
    run_root_query('INSERT INTO location.btle_transmitter_location_index'
                   ' (SELECT id, chain_id, Point(latitude, longitude) FROM location.location'
                   ' WHERE chain_id IS NOT NULL AND hidden=0 AND (miss_count <= 30 OR miss_count IS NULL)'
                   ' AND id IN (SELECT location_id FROM location.btle_transmitters))', reraise=False)
    
    print "Rebuilding gps_check_params_location_index"
    run_root_query('truncate table location.gps_checkin_params_location_index', reraise=False)
    run_root_query('INSERT INTO location.gps_checkin_params_location_index'
                   ' (SELECT id, chain_id, Point(latitude, longitude) FROM location.location'
                   ' WHERE chain_id IS NOT NULL AND hidden=0 AND (miss_count <= 30 OR miss_count IS NULL)'
                   ' AND id IN (SELECT location_id FROM location.gps_checkin_params))', reraise=False)

    # Populate zone_index
    print "Truncating old zone index"
    run_root_query('truncate table zones.zone_index', reraise=False)
    
    print "Rebuilding zone index"
    run_root_query('INSERT into zones.zone_index (select zone_id, Point(latitude, longitude) from zones.zone)', reraise=False)

    """
    if FLAGS.populate_should_update_zookeeper_nodes:
      #Populate zookeeper nodes for versioned tables using the prod_cleanup ETL step.
      print "Cleaning up versioned ETL tables...",
      cleanup_runner = cleanup_versioned_tables.CleanupRunner(
          cleanup_versioned_tables.get_versioned_tables_configs()
      )
      cleanup_runner.cleanup()
      print "DONE"
    """

    print "Populate finished. Congratulations!"


if __name__ == '__main__':
    try:
        populate()
    except Exception:
        import traceback
        traceback.print_exc()
    import time
    time.sleep(600000)
