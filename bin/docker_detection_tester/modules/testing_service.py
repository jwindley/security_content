
import re
import shutil

#import ansible_runner
import yaml
import uuid
import sys
import os
import time
import requests
from modules.DataManipulation import DataManipulation
from modules import utils
from modules import splunk_sdk
import timeit
from typing import Union, Tuple
from os.path import relpath
from tempfile import mkdtemp, mkstemp
import datetime
import http.client
import splunklib.client as client

def load_file(file_path):
    try:

        with open(file_path, 'r', encoding="utf-8") as stream:
            try:
                file = list(yaml.safe_load_all(stream))[0]
            except yaml.YAMLError as exc:
                raise(Exception("ERROR: parsing YAML for {0}:[{1}]".format(file_path, str(exc))))
    except Exception as e:
        raise(Exception("ERROR: opening {0}:[{1}]".format(file_path, str(e))))
    return file





def get_service(splunk_ip:str, splunk_port:int, splunk_password:str):

    try:
        service = client.connect(
            host=splunk_ip,
            port=splunk_port,
            username='admin',
            password=splunk_password
        )
    except Exception as e:
        raise(Exception("Unable to connect to Splunk instance: " + str(e)))
    return service


def execute_tests(splunk_ip:str, splunk_port:int, splunk_password:str, tests:list[dict], attack_data_folder:str, wait_on_failure:bool, wait_on_completion:bool)->list[dict]:
        results = []
        for test in tests:
            try:
                #Run all the tests, even if the test fails.  We still want to get the results of failed tests
                results.append(execute_test(splunk_ip, splunk_port, splunk_password, test, attack_data_folder, wait_on_failure, wait_on_completion))
            except Exception as e:
                raise(Exception(f"Unknown error executing test: {str(e)}"))
        return results
            

def format_test_result(job_result:dict, testName:str, fileName:str, logic:bool=False, noise:bool=False)->dict:
    testResult = {
        "name": testName,
        "file": fileName,
        "logic": logic,
        "noise": noise,
    }


    if 'status' in job_result:
        #Test failed, no need for further processing
        testResult['status'] = job_result['status']
    
    
        
    else:
       #Mark whether or not the test passed
        if job_result['eventCount'] == 1:
            testResult["status"] = True
        else:
            testResult["status"] = False


    JOB_FIELDS = ["runDuration", "scanCount", "eventCount", "resultCount", "performance", "search", "message", "baselines"]
    #Populate with all the fields we want to collect
    for job_field in JOB_FIELDS:
        if job_field in job_result:
            testResult[job_field] = job_result.get(job_field, None)
    
    return testResult

def execute_baselines(splunk_ip:str, splunk_port:int, splunk_password:str, baselines:list[dict])->Tuple[bool,list[dict]]:
    baseline_results = []
    for baseline in baselines:
        formatted_result = execute_baseline(splunk_ip, splunk_port, splunk_password, baseline)        
        baseline_results.append(formatted_result)
        if formatted_result['status'] == False:
            # Fast fail - if a single baseline fails, then it is highly likely that
            # a subsequent baseline will fail, so don't even run it
            return True, baseline_results

    return False, baseline_results

def execute_baseline(splunk_ip:str, splunk_port:int, splunk_password:str, baseline:dict)->dict:
    baseline_file = load_file(os.path.join(os.path.dirname(__file__), '../security_content', baseline['file']))
    result = splunk_sdk.test_detection_search(splunk_ip, splunk_port, splunk_password, 
                                                baseline['search'], baseline['pass_condition'], 
                                            baseline_file['name'], baseline['file'], 
                                            baseline['earliest_time'], baseline['latest_time'])
    
    return format_test_result(result, baseline['name'], baseline['file'])
    
def execute_test(splunk_ip:str, splunk_port:int, splunk_password:str, test:dict, attack_data_folder:str, wait_on_failure:bool, wait_on_completion:bool)->dict:
    print(f"\tExecuting test {test['name']}")
    
    

    #replay all of the attack data
    test_indices = replay_attack_data_files(splunk_ip, splunk_port, splunk_password, test['attack_data'], attack_data_folder)

    
    error = False
    #Run the baseline(s) if they exist for this test
    baselines = None
    if 'baseline' in test:
        error, baselines = execute_baselines(splunk_ip, splunk_port, splunk_password, test['baselines'])

    
    if error == False:
        detection = load_file(os.path.join(os.path.dirname(__file__), '../security_content/detections', test['file']))
        job_result = splunk_sdk.test_detection_search(splunk_ip, splunk_port, splunk_password, detection['search'], test['pass_condition'], detection['name'], test['file'], test['earliest_time'], test['latest_time'])
        result = format_test_result(job_result,test['name'],test['file'])

    else:
        result = format_test_result({"status":False, "message":"Baseline failed"},test['name'],test['file'])

    if baselines is not None:
        result['baselines'] = baselines
 
 
    
    if wait_on_completion or (wait_on_failure and (result['status'] == False)):
        # The user wants to debug the test
        message_template = "\n\n\n****SEARCH {status} : Allowing time to debug search/data****"
        if result['status'] == False:
            # The test failed
            message_template.format(status="FAILURE")
            
        else:
            #The test passed 
            message_template.format(status="SUCCESS")
    
        _ = input(message_template)

    splunk_sdk.delete_attack_data(splunk_ip, splunk_password, splunk_port, indices = test_indices)
    
    
    return result

def replay_attack_data_file(splunk_ip:str, splunk_port:int, splunk_password:str, attack_data_file:dict, attack_data_folder:str)->str:
    """Function to replay a single attack data file. Any exceptions generated during executing
    are intentionally not caught so that they can be caught by the caller.

    Args:
        splunk_ip (str): ip address of the splunk server to target
        splunk_port (int): port of the splunk server API
        splunk_password (str): password to the splunk server
        attack_data_file (dict): a dict containing information about the attack data file
        attack_data_folder (str): The folder for downloaded or copied attack data to reside

    Returns:
        str: index that the attack data has been replayed into on the splunk server
    """
    #Get the index we should replay the data into
    target_index = attack_data_file.get("custom_index", splunk_sdk.DEFAULT_DATA_INDEX)

    descriptor, data_file = mkstemp(prefix="ATTACK_DATA_FILE_", dir=attack_data_folder)
    if not attack_data_file['file_name'].startswith("https://"):
        #raise(Exception(f"Attack Data File {attack_data_file['file_name']} does not start with 'https://'. "  
        #                 "In the future, we will add support for non https:// hosted files, such as local files or other files. But today this is an error."))
        
        #We need to do this because if we are working from a file, we can't overwrite/modify the original during a test. We must keep it intact.
        import shutil
        shutil.copyfile(attack_data_file['file_name'], data_file)
        
    
    else:
        #Download the file
        utils.download_file_from_http(attack_data_file['data'], data_file)
    
    # Update timestamps before replay
    if attack_data_file.get('update_timestamp', False):
        data_manipulation = DataManipulation()
        data_manipulation.manipulate_timestamp(data_file, attack_data_file['sourcetype'], attack_data_file['source'])    

    #Get an session from the API
    service = get_service(splunk_ip, splunk_port, splunk_password)
    #Get the index we will be uploading to
    upload_index = service.indexes[target_index]
        
    #Upload the data
    with open(data_file, 'rb') as target:
        upload_index.submit(target.read(), sourcetype=attack_data_file['sourcetype'], source=attack_data_file['source'], host=splunk_sdk.DEFAULT_EVENT_HOST)

    #Wait for the indexing to finish
    if not splunk_sdk.wait_for_indexing_to_complete(splunk_ip, splunk_port, splunk_password, attack_data_file['sourcetype'], upload_index):
        raise Exception("There was an error waiting for indexing to complete.")
    
    #Return the name of the index that we uploaded to
    return upload_index




    

def replay_attack_data_files(splunk_ip:str, splunk_port:int, splunk_password:str, attack_data_files:list[dict], attack_data_folder:str)->set[str]:
    """Replay all attack data files into a splunk server as part of testing a detection. Note that this does not catch
    any exceptions, they should be handled by the caller

    Args:
        splunk_ip (str): ip address of the splunk server to target
        splunk_port (int): port of the splunk server API
        splunk_password (str): password to the splunk server
        attack_data_files (list[dict]): A list of dicts containing information about the attack data file
        attack_data_folder (str): The folder for downloaded or copied attack data to reside
    """
    test_indices = set()
    for attack_data_file in attack_data_files:
        try:
            test_indices.add(replay_attack_data_file(splunk_ip, splunk_port, splunk_password, attack_data_file, attack_data_folder))
        except Exception as e:
            raise(Exception(f"Error replaying attack data file {attack_data_file['file_name']}: {str(e)}"))
    return test_indices

def test_detection(splunk_ip:str, splunk_port:int, splunk_password:str, test_file:str, attack_data_root_folder, wait_on_failure:bool, wait_on_completion:bool)->list[dict]:
    
    #Raises exception if it doesn't find the file
    test_file_obj = load_file(os.path.join("security_content/", test_file))
    
        

    abs_folder_path = mkdtemp(prefix="DATA_", dir=attack_data_root_folder)
    results = execute_tests(splunk_ip, splunk_port, splunk_password, test_file_obj['tests'], abs_folder_path, wait_on_failure, wait_on_completion)
    #Delete the folder and all of the data inside of it
    shutil.rmtree(abs_folder_path)


    return results






