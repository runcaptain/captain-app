"""Run synthetic contracts on an isolated UTF8 PostgreSQL cluster, then stop it."""
import os, pathlib, subprocess, tempfile, sys
root = pathlib.Path(tempfile.mkdtemp(prefix='cst-pg-', dir='/tmp'))
root.chmod(0o700)
bin = os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@17/bin').rstrip('/') + '/'
os.chdir(pathlib.Path(__file__).resolve().parents[3])
started = False
try:
    subprocess.run([bin+'initdb', '-D', str(root/'data'), '--auth-local=trust', '--auth-host=reject', '--no-locale', '--encoding=UTF8'], check=True, capture_output=True)
    subprocess.run([bin+'pg_ctl', '-D', str(root/'data'), '-l',str(root/'postgres.log'), '-o',f"-k {root} -h '' -p 55489", '-w','start'], check=True, capture_output=True)
    started = True
    subprocess.run([bin+'createdb','-h',str(root),'-p','55489','captain_sync_test'], check=True)
    env = dict(os.environ, PYTHONPATH='backend/core', DATABASE_URL=f'postgresql+psycopg:///{"captain_sync_test"}?host={root}&port=55489')
    result = subprocess.run([sys.executable,'-m','pytest','backend/core/tests/test_team_operator.py','experiments/team-sync/test_server.py','experiments/team-sync/test_invariants.py','experiments/team-sync/test_jobs.py','experiments/team-sync/test_artifacts.py','-q','--tb=short','--maxfail=1'],env=env)
    print('ISOLATED_POSTGRES_TEST_EXIT',result.returncode,flush=True)
    raise SystemExit(result.returncode)
finally:
    if started:
        subprocess.run([bin+'pg_ctl','-D',str(root/'data'),'-m','fast','-w','stop'],check=True,capture_output=True)
        print('ISOLATED_POSTGRES_STOPPED',str(root),flush=True)
