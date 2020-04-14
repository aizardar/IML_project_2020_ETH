import pandas as pd
import tensorflow as tf
import models
from models import simple_model, threelayers
import numpy as np
from sklearn.model_selection import train_test_split, ParameterGrid
from sklearn import preprocessing, svm
from matplotlib import pyplot as plt
import sklearn.metrics as metrics
import os
import utils
import functools, operator
from models import dice_coef_loss, build_model
from tqdm import tqdm
from scipy.spatial.distance import dice
import kerastuner
from kerastuner import RandomSearch
"""
Predict whether medical tests are ordered by a clinician in the remainder of the hospital stay: 0 means that there will be no further tests of this kind ordered, 1 means that at least one of a test of that kind will be ordered. In the submission file, you are asked to submit predictions in the interval [0, 1], i.e., the predictions are not restricted to binary. 0.0 indicates you are certain this test will not be ordered, 1.0 indicates you are sure it will be ordered. The corresponding columns containing the binary groundtruth in train_labels.csv are: LABEL_BaseExcess, LABEL_Fibrinogen, LABEL_AST, LABEL_Alkalinephos, LABEL_Bilirubin_total, LABEL_Lactate, LABEL_TroponinI, LABEL_SaO2, LABEL_Bilirubin_direct, LABEL_EtCO2.
10 labels for this subtask

fill base excess with 0
fibrogen with -1

Questions:
    - include time axis in training data?
    - use a SVM for each value to predict?
"""
test = False
seed = 10
batch_size = 2048
num_subjects = -1         #number of subjects out of 18995
epochs = 1000
TRIALS = 50

search_space_dict = {
    'loss': ['dice','binary_crossentropy'],
    'nan_handling': ['minusone'],
    'standardizer': ['none'],
    'output_layer': ['sigmoid'],
    'model': ['threelayers'],
}
test = True
search_space_dict = {
    'loss': ['binary_crossentropy'],
    'nan_handling': ['minusone'],
    'standardizer': ['none'],
    'output_layer': ['sigmoid'],
    'model': ['threelayers'],
    'keras_tuner': ['True'],
}

if not os.path.isfile('temp/params_results.csv'):
    columns = [key for key in search_space_dict.keys()]
    columns.append('roc_auc')
    params_results_df = pd.DataFrame(columns=columns)
else:
    params_results_df = pd.read_csv('temp/params_results.csv')
    for key in search_space_dict.keys():
        if not key in list(params_results_df.columns):
            params_results_df[key] = np.nan

search_space = list(ParameterGrid(search_space_dict))
y_train_df = pd.read_csv('train_labels.csv').sort_values(by='pid')
y_train_df = y_train_df.iloc[:num_subjects, :10 + 1]

if not os.path.isfile('xtrain_imputedNN{}.csv'.format(num_subjects)):
    X_train_df = pd.read_csv('train_features.csv').sort_values(by='pid')
    X_train_df = X_train_df.loc[X_train_df['pid'] < y_train_df['pid'].values[-1] + 1]
    X_train_df = utils.impute_NN(X_train_df)
    # X_train_df['BaseExcess'].fillna(0)
    X_train_df.to_csv('xtrain_imputedNN{}.csv'.format(num_subjects), index = False)
else:
    X_train_df = pd.read_csv('xtrain_imputedNN{}.csv'.format(num_subjects))


def test_model(params, X_train_df, y_train_df):
    print('\n', params)
    path = 'nan_handling/{}_{}'.format(num_subjects, params['nan_handling'])
    if os.path.isfile(path):
        X_train_df = pd.read_csv(path)
    else:
        if params['nan_handling'] == 'iterative':
            try:
                from sklearn.experimental import enable_iterative_imputer
                from sklearn.impute import IterativeImputer, SimpleImputer
            except Exception as E:
                print(E)
                return np.nan
        X_train_df = utils.handle_nans(X_train_df, params, seed)
        X_train_df.to_csv(path, index = False)
    loss = params['loss']
    if loss == 'dice':
        loss = dice_coef_loss

    """
    Scaling data
    """
    if not params['standardizer'] == 'none':
        scaler = utils.scaler(params)
        x_train_df = pd.DataFrame(data = scaler.fit_transform(X_train_df.values[:, 1:]), columns = X_train_df.columns[1:])
        x_train_df.insert(0, 'pid', X_train_df['pid'].values)
    else:
        x_train_df = X_train_df
    # x_train_df.to_csv('temp/taining_data.csv')

    x_train = []
    for i, subject in enumerate(list(dict.fromkeys(x_train_df['pid'].values.tolist()))):
        if X_train_df.loc[X_train_df['pid'] == subject].values[:, 1:].shape[0] > 12:
            raise Exception('more than 12 time-points')
        x_train.append(X_train_df.loc[X_train_df['pid'] == subject].values[:, 1:])
    input_shape = x_train[0].shape
    y_train = list(y_train_df.values[:, 1:])

    """
    Splitting the dataset into train 60%, val 30% and test 10% 
    """
    x_train, x_valtest, y_train, y_valtest = train_test_split(x_train, y_train, test_size=0.4, random_state=seed)
    x_val, x_test, y_val, y_test = train_test_split(x_valtest, y_valtest, test_size=0.3, random_state=seed)

    """
    Making datasets
    """
    train_dataset = tf.data.Dataset.from_tensor_slices((x_train, y_train))
    train_dataset = train_dataset.shuffle(len(x_train)).batch(batch_size=batch_size).repeat()
    val_dataset = tf.data.Dataset.from_tensor_slices((x_val, y_val))
    val_dataset = val_dataset.shuffle(len(x_val)).batch(batch_size=batch_size).repeat()
    test_dataset = tf.data.Dataset.from_tensor_slices(x_test)
    test_dataset = test_dataset.batch(batch_size=1)

    """
    Callbacks 
    """
    CB_lr = tf.keras.callbacks.ReduceLROnPlateau(monitor='val_loss',
        patience= 5,
        verbose=1,
        min_delta=0.0001,
        min_lr= 1e-6)

    CB_es = tf.keras.callbacks.EarlyStopping(monitor='val_loss',
        min_delta= 0.0001,
        verbose= 1,
        patience= 10,
        mode='min',
        restore_best_weights=True)
    callbacks = [CB_es, CB_lr]
    if params['keras_tuner'] == 'False':
        if params['model'] == 'threelayers':
            model = threelayers(input_shape, loss, params['output_layer'])
        elif params['model'] == 'svm':
            model = models.svm(input_shape, loss, params['output_layer'])
        model.fit(train_dataset, validation_data=val_dataset, epochs=epochs, steps_per_epoch=len(x_train) // batch_size,
                  validation_steps=len(x_train) // batch_size, callbacks=callbacks)
    elif params['keras_tuner'] == 'True':
        print(input_shape)
        tuner = RandomSearch(build_model, objective= kerastuner.Objective("val_auc", direction="min"), max_trials=TRIALS,
                             project_name='subtask1_results')
        tuner.search_space_summary()
        tuner.search(train_dataset, validation_data = val_dataset, epochs = epochs, steps_per_epoch=len(x_train)//batch_size, validation_steps = len(x_train)//batch_size, callbacks = callbacks)
        tuner.results_summary()

        # Retrieve the best model and display its architecture
        model = tuner.get_best_models(num_models=1)[0]
        # model.summary()

    prediction = model.predict(test_dataset)
    prediction_df = pd.DataFrame(prediction, columns= y_train_df.columns[1:])
    # prediction_df.to_csv('temp/result.csv')
    y_test_df = pd.DataFrame(y_test, columns= y_train_df.columns[1:])
    # y_test_df.to_csv('temp/ytrue.csv')
    dice_score = [1 - dice(y_test_df[entry], np.where(prediction_df[entry] > 0.5, 1, 0)) for entry in y_train_df.columns[1:]]
    mean_dice = np.mean(dice_score)
    roc_auc = [metrics.roc_auc_score(y_test_df[entry], prediction_df[entry]) for entry in y_train_df.columns[1:]]
    mean_roc_auc = np.mean(roc_auc)
    return roc_auc, mean_roc_auc, dice_score, mean_dice

for params in tqdm(search_space):
    a = params_results_df.loc[(), 'roc_auc']
    temp_df = params_results_df.loc[functools.reduce(operator.and_, (params_results_df['{}'.format(item)] == params['{}'.format(item)] for item in search_space_dict.keys())), 'roc_auc']
    not_tested = temp_df.empty or temp_df.isna().all()
    if not_tested or test == True:
        df = pd.DataFrame.from_records([params])
        scores = test_model(params, X_train_df, y_train_df)
        for column, score in zip(['roc_auc', 'mean_roc_auc', 'dice_score', 'mean_dice'],scores):
            if type(score) == list:
                df[column] = -1
                df[column] = df[column].astype('object')
                df.at[0, column] = score
            else:
                df[column] = score
        print(df)
        params_results_df = params_results_df.append(df, sort= False)
    else:
        print('already tried this combination: ', params)
    if not test == True:
        params_results_df.to_csv('temp/params_results.csv', index= False)




# best = fmin(fn = test_model, space = search_space, algo = hp.tpe.suggest, max_evals = MAX_EVALS, trials = bayes_trials)
# pd.DataFrame(best, index=[0]).to_csv('best_params.csv')

