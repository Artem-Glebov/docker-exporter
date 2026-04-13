from docker import from_env
from prometheus_client import start_http_server, Gauge, Enum
import time

docker_client = from_env()

PORT = 8088

CONTAINER_STATUS = Gauge(
    'docker_container_status',
    'Container status (1 = runnibg, 0 = stopped, -1 = error)',
    ['container_id', 'container_name', 'image']
)

CONTAINER_RESTARTS = Gauge(
    'docker_container_restarts_total',
    'Number of container restarts',
    ['container_id', 'container_name', 'image']
)

# CONTAINER_EXIT_CODE = Gauge(
#     'docker_container_exit_code',
#     'Exit code)',
#     ['container_id', 'container_name', 'image']
# )

# CONTAINER_ERROR = Gauge(
#     'docker_container_error',
#     'Container error status (1 = error, 0 = no error)',
#     ['container_id', 'container_name', 'image', 'exit_code']
# )

# CONTAINER_CRASHED_WITHOUT_RESTART = Enum(
#     'docker_container_crashed_without_restart',
#     'The container crashed and did not restart (1 = crashed without restarting, 0 = restarted or alive)',
#     ['container_id', 'container_name', 'image'],
#     states=['not_crashed', 'crashed_without_restart']
# )

def collect_docker_metrics():
    containers = docker_client.containers.list(all=True)
    current_container_ids = {container.id[:12] for container in containers}

    for metric in [CONTAINER_STATUS, CONTAINER_RESTARTS]:
        for sample in metric.collect():
            for label_value in sample.samples:
                container_id = label_value[1]['container_id']
                if container_id not in current_container_ids:
                    metric.remove(container_id, label_value[1]['container_name'], label_value[1]['image'])
    
    for container in containers:
        container.reload()
        container_id = container.id[:12]
        container_name = container.name

        #image = container.image.tags[0] if container.image.tags else "unknown"
        try:
            image = container.image.tags[0] if container.image.tags else "unknown"
        except Exception as e:
            print(e)
            image = container.attrs['Config']['Image'] or "unknown"

        if container.status == 'running':
            CONTAINER_STATUS.labels(container_id, container_name, image).set(1)
            # CONTAINER_CRASHED_WITHOUT_RESTART.labels(container_id, container_name, image).state('not_crashed')
        elif container.status == 'exited':
            CONTAINER_STATUS.labels(container_id, container_name, image).set(0)

            # restart_policy = container.attrs['HostConfig'].get('RestartPolicy', {}).get('Name')
            # if restart_policy not in ['always', 'unless-stopped']:
            #     CONTAINER_CRASHED_WITHOUT_RESTART.labels(container_id, container_name, image).state('crashed_without_restart')
            # else:
            #     CONTAINER_CRASHED_WITHOUT_RESTART.labels(container_id, container_name, image).state('not_crashed')

        # if container.attrs['HostConfig'].get('RestartPolicy', {}).get('Name') in ['always', 'unless-stopped']:
        #     CONTAINER_RESTARTS.labels(container_id, container_name, image).set(container.attrs['RestartCount'])
        CONTAINER_RESTARTS.labels(container_id, container_name, image).set(
            container.attrs['RestartCount']
        )

        # if container.status == 'exited':
        #     CONTAINER_EXIT_CODE.labels(container_id, container_name, image).set(
        #         container.attrs['State']['ExitCode']
        #     )
        # else:
        #     CONTAINER_EXIT_CODE.labels(container_id, container_name, image).set(-1) 

        # if container.status == 'exited' and container.attrs['State']['ExitCode'] != 0:
        #     CONTAINER_ERROR.labels(container_id, container_name, image, str(container.attrs['State']['ExitCode'])).set(1)
        # else:
        #     CONTAINER_ERROR.labels(container_id, container_name, image, "0").set(0)
                

if __name__ == '__main__':
    start_http_server(int(PORT))
    print(f'Starting server on port {PORT}')

    while True:
        collect_docker_metrics()
        time.sleep(15)
