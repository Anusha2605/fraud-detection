import os
import kfp
from kfp import compiler
from kfp import dsl
from kfp.dsl import InputPath, OutputPath

from kfp import kubernetes


@dsl.component(base_image="quay.io/modh/runtime-images:runtime-cuda-tensorflow-ubi9-python-3.9-2024a-20240523")
def get_data(train_data_output_path: OutputPath(), validate_data_output_path: OutputPath()):
    import urllib.request
    print("starting download...")
    print("downloading training data")
    url = "https://raw.githubusercontent.com/rh-aiservices-bu/fraud-detection/main/data/train.csv"
    urllib.request.urlretrieve(url, train_data_output_path)
    print("train data downloaded")
    print("downloading validation data")
    url = "https://raw.githubusercontent.com/rh-aiservices-bu/fraud-detection/main/data/validate.csv"
    urllib.request.urlretrieve(url, validate_data_output_path)
    print("validation data downloaded")


@dsl.component(
    base_image="quay.io/modh/runtime-images:runtime-cuda-tensorflow-ubi9-python-3.9-2024a-20240523",
    packages_to_install=["onnx==1.17.0", "onnxruntime==1.19.2", "tf2onnx==1.16.1"],
)
def train_model(train_data_input_path: InputPath(), validate_data_input_path: InputPath(), model_output_path: OutputPath()):
    import numpy as np
    import pandas as pd
    from keras.models import Sequential
    from keras.layers import Dense, Dropout, BatchNormalization, Activation
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.utils import class_weight
    import tf2onnx
    import onnx
    import pickle
    from pathlib import Path

    # Load the CSV data which we will use to train the model.
    # It contains the following fields:
    #   distancefromhome - The distance from home where the transaction happened.
    #   distancefromlast_transaction - The distance from last transaction happened.
    #   ratiotomedianpurchaseprice - Ratio of purchased price compared to median purchase price.
    #   repeat_retailer - If it's from a retailer that already has been purchased from before.
    #   used_chip - If the (credit card) chip was used.
    #   usedpinnumber - If the PIN number was used.
    #   online_order - If it was an online order.
    #   fraud - If the transaction is fraudulent.


    feature_indexes = [
        1,  # distance_from_last_transaction
        2,  # ratio_to_median_purchase_price
        4,  # used_chip
        5,  # used_pin_number
        6,  # online_order
    ]

    label_indexes = [
        7  # fraud
    ]

    X_train = pd.read_csv(train_data_input_path)
    y_train = X_train.iloc[:, label_indexes]
    X_train = X_train.iloc[:, feature_indexes]

    X_val = pd.read_csv(validate_data_input_path)
    y_val = X_val.iloc[:, label_indexes]
    X_val = X_val.iloc[:, feature_indexes]

    # Scale the data to remove mean and have unit variance. The data will be between -1 and 1, which makes it a lot easier for the model to learn than random (and potentially large) values.
    # It is important to only fit the scaler to the training data, otherwise you are leaking information about the global distribution of variables (which is influenced by the test set) into the training set.

    scaler = StandardScaler()

    X_train = scaler.fit_transform(X_train.values)

    Path("artifact").mkdir(parents=True, exist_ok=True)
    with open("artifact/scaler.pkl", "wb") as handle:
        pickle.dump(scaler, handle)

    # Since the dataset is unbalanced (it has many more non-fraud transactions than fraudulent ones), set a class weight to weight the few fraudulent transactions higher than the many non-fraud transactions.
    class_weights = class_weight.compute_class_weight('balanced', classes=np.unique(y_train), y=y_train.values.ravel())
    class_weights = {i: class_weights[i] for i in range(len(class_weights))}

    # Build the model, the model we build here is a simple fully connected deep neural network, containing 3 hidden layers and one output layer.

    model = Sequential()
    model.add(Dense(32, activation='relu', input_dim=len(feature_indexes)))
    model.add(Dropout(0.2))
    model.add(Dense(32))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(Dropout(0.2))
    model.add(Dense(32))
    model.add(BatchNormalization())
    model.add(Activation('relu'))
    model.add(Dropout(0.2))
    model.add(Dense(1, activation='sigmoid'))
    model.compile(optimizer='adam', loss='binary_crossentropy', metrics=['accuracy'])
    model.summary()

    # Train the model and get performance

    epochs = 2
    history = model.fit(X_train, y_train, epochs=epochs,
                        validation_data=(scaler.transform(X_val.values), y_val),
                        verbose=True, class_weight=class_weights)

    # Save the model as ONNX for easy use of ModelMesh
    model_proto, _ = tf2onnx.convert.from_keras(model)
    print(model_output_path)
    onnx.save(model_proto, model_output_path)


@dsl.component(
    base_image="quay.io/modh/runtime-images:runtime-cuda-tensorflow-ubi9-python-3.9-2024a-20240523",
    packages_to_install=["boto3==1.35.55", "botocore==1.35.55"]
)
def upload_model(input_model_path: InputPath()):
    import os
    import boto3
    import botocore

    aws_access_key_id = os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    endpoint_url = os.environ.get('AWS_S3_ENDPOINT')
    region_name = os.environ.get('AWS_DEFAULT_REGION')
    bucket_name = os.environ.get('AWS_S3_BUCKET')

    s3_key = os.environ.get("S3_KEY")

    session = boto3.session.Session(aws_access_key_id=aws_access_key_id,
                                    aws_secret_access_key=aws_secret_access_key)

    s3_resource = session.resource(
        's3',
        config=botocore.client.Config(signature_version='s3v4'),
        endpoint_url=endpoint_url,
        region_name=region_name)

    bucket = s3_resource.Bucket(bucket_name)

    print(f"Uploading {s3_key}")
    bucket.upload_file(input_model_path, s3_key)


@dsl.pipeline(name=os.path.basename(__file__).replace('.py', ''))
def pipeline():
    get_data_task = get_data()
    train_data_csv_file = get_data_task.outputs["train_data_output_path"]
    validate_data_csv_file = get_data_task.outputs["validate_data_output_path"]

    train_model_task = train_model(train_data_input_path=train_data_csv_file,
                                   validate_data_input_path=validate_data_csv_file)
    onnx_file = train_model_task.outputs["model_output_path"]

    upload_model_task = upload_model(input_model_path=onnx_file)

    upload_model_task.set_env_variable(name="S3_KEY", value="models/fraud/1/model.onnx")

    kubernetes.use_secret_as_env(
        task=upload_model_task,
        secret_name='frauddetection-storage',
        secret_key_to_env={
            'AWS_ACCESS_KEY_ID': 'AWS_ACCESS_KEY_ID',
            'AWS_SECRET_ACCESS_KEY': 'AWS_SECRET_ACCESS_KEY',
            'AWS_DEFAULT_REGION': 'AWS_DEFAULT_REGION',
            'AWS_S3_BUCKET': 'AWS_S3_BUCKET',
            'AWS_S3_ENDPOINT': 'AWS_S3_ENDPOINT',
        })

if __name__ == "__main__":
    kubeflow_endpoint = os.environ['KUBEFLOW_ENDPOINT']
    print(f"Connecting to kfp: {kubeflow_endpoint}")

    sa_token_path = "/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105
    #if config("BEARER_TOKEN"):
    #    bearer_token = config("BEARER_TOKEN")
    if os.path.isfile(sa_token_path):
        with open(sa_token_path) as f:
            bearer_token = f.read().rstrip()

    # Check if the script is running in a k8s pod
    # Get the CA from the service account if it is
    # Skip the CA if it is not
    sa_ca_cert = "/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"
    if os.path.isfile(sa_ca_cert) and "svc" in kubeflow_endpoint:
        ssl_ca_cert = sa_ca_cert
    else:
        ssl_ca_cert = None
        print("there is no ssl_ca_cert")
    print(kubeflow_endpoint)

    print(bearer_token)
    print(ssl_ca_cert)
    client = kfp.Client(
        host=kubeflow_endpoint,
        existing_token=bearer_token,
        ssl_ca_cert=ssl_ca_cert,
    )
    result = client.create_run_from_pipeline_func(pipeline, arguments={}, experiment_name="fraud-detection")
    print(f"Starting pipeline run with run_id: {result.run_id}")


