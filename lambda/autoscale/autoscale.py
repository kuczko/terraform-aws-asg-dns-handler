import json
import logging
import boto3
import sys

logger = logging.getLogger()
logger.setLevel(logging.INFO)

autoscaling = boto3.client('autoscaling')
ec2 = boto3.client('ec2')
route53 = boto3.client('route53')

HOSTNAME_TAG_NAME = "asg:hostname_pattern"

LIFECYCLE_KEY = "LifecycleHookName"
ASG_KEY = "AutoScalingGroupName"

# Fetches private IP of an instance via EC2 API
def fetch_private_ip_from_ec2(instance_id):
    logger.info("Fetching private IP for instance-id: %s", instance_id)

    ec2_response = ec2.describe_instances(InstanceIds=[instance_id])
    try:
      ip_address = ec2_response['Reservations'][0]['Instances'][0]['NetworkInterfaces'][0]['PrivateIpAddress']
      logger.info("Found private IP for instance-id %s: %s", instance_id, ip_address)
    except:
      return ""
    return ip_address

def get_zone_name(zone_id):
   zone_response = route53.get_hosted_zone(Id=zone_id)
   zone_name = zone_response['HostedZone']['Name']
 
   return zone_name

# Fetches private IP of an instance via route53 API
def fetch_private_ip_from_route53(hostname, zone_id):
    logger.info("Fetching private IP for hostname: %s", hostname)

    dns_name = get_zone_name(zone_id)
    dns_hostname = hostname + "." + dns_name

    try:
      ip_address = route53.list_resource_record_sets(
          HostedZoneId=zone_id,
          StartRecordName=dns_hostname,
          StartRecordType='A',
          MaxItems='1'
      )['ResourceRecordSets'][0]['ResourceRecords'][0]['Value']

      logger.info("Found private IP for hostname %s: %s", hostname, ip_address)
    except:
      return ""
    return ip_address

# Fetches relevant tags from ASG
# Returns tuple of hostname_pattern, zone_id
def fetch_tag_metadata(asg_name):
    logger.info("Fetching tags for ASG: %s", asg_name)

    tag_value = autoscaling.describe_tags(
        Filters=[
            {'Name': 'auto-scaling-group','Values': [asg_name]},
            {'Name': 'key','Values': [HOSTNAME_TAG_NAME]}
        ],
        MaxRecords=1
    )['Tags'][0]['Value']

    logger.info("Found tags for ASG %s: %s", asg_name, tag_value)

    return tag_value.split("@")

# Returns hostname from ec2 tag
def fetch_hostname_from_ec2(instance_id):
    logger.info("Checking Name tag for %s", instance_id)
    my_tags = ec2.describe_tags(
        Filters = [
           {'Name':'resource-id','Values':[ instance_id ]},
           {'Name':'key','Values':['Name']}
        ]
    )
    logger.info("Got %s",my_tags['Tags'])
    return my_tags['Tags'][0]['Value']

# Returns first availabie counter of dns entries in ASG
def fetch_first_available_count(hostname_pattern,instance_id, zone_id, asg_name):
    asg_description = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames = [asg_name])
    logger.info("ASG Instances: %s",asg_description['AutoScalingGroups'][0]['Instances'])
    my_counter = 1
    while True:
      new_hostname = hostname_pattern.replace('#counter',str(my_counter))
      logger.info("Testing %s",new_hostname)
      private_ip = fetch_private_ip_from_route53(new_hostname,zone_id)
      if private_ip == "":
          logger.info("No ip for %s - success",new_hostname)
          return my_counter
      my_instance = ec2.describe_instances(
          Filters = [
              {'Name':'private-ip-address','Values':[private_ip]},
              {'Name':'instance-state-name','Values':['pending','running']}
          ]
      )
      logger.info("Instance data: %s",my_instance)
      if len(my_instance['Reservations']) == 0:
         logger.info("Reservations are empty - success")
         return my_counter
      my_counter += 1
    return 0


# Builds a hostname according to pattern
def build_hostname(hostname_pattern, instance_id, zone_id, asg_name):
    #new_hostname = hostname_pattern.replace('#instanceid', instance_id)
    first_count = fetch_first_available_count(hostname_pattern,instance_id, zone_id, asg_name)
    new_hostname = hostname_pattern.replace('#counter', str(first_count))
    return new_hostname

# Updates the name tag of an instance
def update_name_tag(instance_id, hostname):
    tag_name = hostname.split('.')[0]
    logger.info("Updating name tag for instance-id %s with: %s", instance_id, tag_name)
    ec2.create_tags(
        Resources = [
            instance_id
        ],
        Tags = [
            {
                'Key': 'Name',
                'Value': tag_name
            }
        ]
    )

# Updates a Route53 record
def update_record(zone_id, ip, hostname, operation):
    logger.info("Changing record with %s for %s -> %s in %s", operation, hostname, ip, zone_id)
    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            'Changes': [
                {
                    'Action': operation,
                    'ResourceRecordSet': {
                        'Name': hostname,
                        'Type': 'A',
                        'TTL': 300,
                        'ResourceRecords': [{'Value': ip}]
                    }
                }
            ]
        }
    )

# Processes a scaling event
# Builds a hostname from tag metadata, fetches a private IP, and updates records accordingly
def process_message(message):
    if message['Event'] == "autoscaling:EC2_INSTANCE_LAUNCH":
        operation = "UPSERT"
    elif message['Event'] == "autoscaling:EC2_INSTANCE_TERMINATE" or message['Event'] == "autoscaling:EC2_INSTANCE_LAUNCH_ERROR":
        operation = "DELETE"
    else:
        logger.error("Encountered unknown event type: %s", message['LifecycleTransition'])

    asg_name = message['AutoScalingGroupName']
    instance_id =  message['EC2InstanceId']

    hostname_pattern, zone_id = fetch_tag_metadata(asg_name)
    hostname = build_hostname(hostname_pattern, instance_id, zone_id, asg_name)
    logger.info("Builded hostname: %s", hostname)
    dns_name = get_zone_name(zone_id)
    dns_hostname = hostname + "." + dns_name

    if operation == "UPSERT":
        private_ip = fetch_private_ip_from_ec2(instance_id)

        update_name_tag(instance_id, dns_hostname)
        update_record(zone_id, private_ip, dns_hostname, operation)
#    else:
#        private_ip = fetch_private_ip_from_route53(dns_hostname, zone_id)


# Picks out the message from a SNS message and deserializes it
def process_record(record):
    process_message(json.loads(record['Sns']['Message']))

# Main handler where the SNS events end up to
# Events are bulked up, so process each Record individually
def lambda_handler(event, context):
    logger.info("Processing SNS event: " + json.dumps(event))

    for record in event['Records']:
        process_record(record)

# Finish the asg lifecycle operation by sending a continue result
    logger.info("Finishing ASG action")
    message =json.loads(record['Sns']['Message'])
    if LIFECYCLE_KEY in message and ASG_KEY in message :
        response = autoscaling.complete_lifecycle_action (
            LifecycleHookName = message['LifecycleHookName'],
            AutoScalingGroupName = message['AutoScalingGroupName'],
            InstanceId = message['EC2InstanceId'],
            LifecycleActionToken = message['LifecycleActionToken'],
            LifecycleActionResult = 'CONTINUE'
        
        )
        logger.info("ASG action complete: %s", response)    
    else :
        logger.error("No valid JSON message")        

# if invoked manually, assume someone pipes in a event json
if __name__ == "__main__":
    logging.basicConfig()

    lambda_handler(json.load(sys.stdin), None)
