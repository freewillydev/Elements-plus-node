#!/usr/bin/env python3
"""Exercise Elements' native reader against two isolated ECX regtest parents.

Requires an ECX bitcoind and a built test_elements. No wallets or public peers.
All data is retained under a newly created directory; subprocesses stop on exit.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time


def wait_for(predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.2)
    raise TimeoutError('isolated parent did not reach expected state')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', type=Path, required=True)
    parser.add_argument('--bitcoin-cli', type=Path, required=True)
    parser.add_argument('--test-elements', type=Path, required=True)
    parser.add_argument('--workdir', type=Path, required=True)
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='pruned-parent-', dir=args.workdir))
    sockets = []
    ports = []
    for _ in range(4):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        ports.append(sock.getsockname()[1])
        sockets.append(sock)
    nodes = []
    for index in range(2):
        data = root / str(index)
        data.mkdir()
        (data / 'bitcoin.conf').write_text(
            'regtest=1\nserver=1\ndisablewallet=1\ndiscover=0\ndnsseed=0\n'
            'connect=0\nlistenonion=0\nupnp=0\nnatpmp=0\nrpcuser=fixture\nrpcpassword=fixture\n'
            'fallbackfee=0.00001\n' + ('prune=1\nfastprune=1\n' if index else '') +
            f'[regtest]\nrpcbind=127.0.0.1\nrpcallowip=127.0.0.1\n'
            f'rpcport={ports[index * 2]}\nbind=127.0.0.1:{ports[index * 2 + 1]}\n')
        nodes.append({'data': data, 'process': None})
    for sock in sockets:
        sock.close()

    def rpc(index, method, *params):
        result = subprocess.run([str(args.bitcoin_cli), f'-datadir={nodes[index]["data"]}',
                                 f'-conf={nodes[index]["data"] / "bitcoin.conf"}', '-regtest', method, *(str(p) for p in params)],
                                capture_output=True, text=True, timeout=15, check=True)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()

    def start(index):
        node = nodes[index]
        with (node['data'] / 'console.log').open('a') as log:
            node['process'] = subprocess.Popen([str(args.bitcoind), f'-datadir={node["data"]}',
                                               f'-conf={node["data"] / "bitcoin.conf"}', '-regtest',
                                               '-connect=0', '-dnsseed=0', '-discover=0'],
                                               stdout=log, stderr=subprocess.STDOUT)
        wait_for(lambda: node['process'].poll() is None and rpc(index, 'getblockchaininfo')['chain'] == 'regtest')

    def stop(index):
        process = nodes[index]['process']
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def connected():
        return rpc(1, 'getbestblockhash') == rpc(0, 'getbestblockhash')

    def recover(height, label, expected_error='', block_hash=None):
        block_hash = block_hash or rpc(0, 'getblockhash', height)
        metadata = rpc(0, 'getblockheader', block_hash)
        env = {key: value for key, value in os.environ.items() if not key.startswith('ELEMENTS_TEST_')}
        env.update({
            'ELEMENTS_TEST_HEADER_RPC_PORT': str(ports[2]),
            'ELEMENTS_TEST_HEADER_HASH': block_hash,
            'ELEMENTS_TEST_HEADER_GENESIS': rpc(0, 'getblockhash', 0),
            'ELEMENTS_TEST_HEADER_ERROR': expected_error,
            'ELEMENTS_TEST_PARENT_BODY': 'full',
            'ELEMENTS_TEST_INDEPENDENT_PARENT': '1',
            'ELEMENTS_TEST_EXPECTED_HEIGHT': str(height),
            'ELEMENTS_TEST_EXPECTED_CONFIRMATIONS': str(metadata['confirmations']),
            'ELEMENTS_TEST_EXPECTED_MTP': str(metadata['mediantime']),
            'ELEMENTS_TEST_EXPECTED_CHAINWORK': metadata['chainwork'],
        })
        with (root / f'{label}.log').open('w') as log:
            subprocess.run([str(args.test_elements),
                            '--run_test=pegin_witness_tests/parent_header_native_http_transport',
                            '--report_level=detailed'], env=env, stdout=log,
                           stderr=subprocess.STDOUT, timeout=60, check=True)
        return block_hash

    results = {'data_directory': str(root), 'checks': [], 'passed': False}
    try:
        start(0)
        rpc(0, 'generatetodescriptor', 1100, 'raw(51)')
        start(1)
        rpc(1, 'addnode', f'127.0.0.1:{ports[1]}', 'onetry')
        wait_for(connected)
        rpc(1, 'pruneblockchain', 500)
        old_hash = rpc(0, 'getblockhash', 10)
        try:
            rpc(1, 'getblock', old_hash, 0)
        except subprocess.CalledProcessError as error:
            assert 'pruned' in error.stderr.lower(), error.stderr
        else:
            raise AssertionError('test block was not actually pruned')
        results['checks'].append('old block confirmed pruned')
        recover(10, 'recovery')
        results['checks'].append('native authenticated recovery from archival peer')
        stop(1)
        start(1)
        rpc(1, 'addnode', f'127.0.0.1:{ports[1]}', 'onetry')
        wait_for(lambda: len(rpc(1, 'getpeerinfo')) > 0)
        recover(20, 'restart-recovery')
        results['checks'].append('native recovery after pruned-parent restart')
        old = recover(1055, 'before-reorg')
        rpc(0, 'disconnectnode', '', rpc(0, 'getpeerinfo')[0]['id'])
        wait_for(lambda: not rpc(0, 'getpeerinfo') and not rpc(1, 'getpeerinfo'))
        rpc(0, 'invalidateblock', rpc(0, 'getblockhash', 1050))
        rpc(0, 'generatetodescriptor', 80, 'raw(52)')
        rpc(1, 'addnode', f'127.0.0.1:{ports[1]}', 'onetry')
        wait_for(connected)
        assert rpc(1, 'getblockheader', old)['confirmations'] == -1
        recover(1055, 'stale-reorg', 'parent block has -1 confirmations', old)
        results['checks'].append('native reader rejects disconnected old branch')
        recover(1055, 'after-reorg')
        results['checks'].append('native authentication of replacement after 51-block reorg')
        results['passed'] = True
    except Exception as error:
        results['passed'] = False
        results['error'] = str(error) + (': ' + error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else '')
        raise
    finally:
        for index in reversed(range(2)):
            stop(index)
        (root / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results, indent=2), flush=True)


if __name__ == '__main__':
    main()
