import torch.optim as optim
import torch
from torch import nn
from torch.autograd import Variable
import numpy as np
from copy import deepcopy


torch.set_num_threads(1)

class Model:
    def __init__(self, model, mode='train', train_X=None, train_y=None, tr_generator=None, num_steps=None, dev_X=None, dev_y=None, model_dir=None, output_dir=None,
                 weight_dir=None, num_epochs=10, patience=4, batch_size=64, min_epochs=2, lr=0.003, lambda_attn=4, verbose=False, use_rationale=False,
                 rationale_with_UNK=1):
        """
        A wrapper class used for model training and predicting.
        Data format: the batch of data is an array of dictionary
                     each dictionary contains a sentence, a label (and optionally a rationale representation)
                     X['word_content_input'] is a list of integers (sequence of tokens indexed)
                     X['rationale_distr'] is a probability distribution generated by rationale annotations
                     X[context_feature_name] is the user's feature for a context feature
        :param model: the model to train and predict
        :param mode: 'train', 'eval'
        :param train_X: training data features
        :param train_y: training data labels
        :param dev_X: validation data features
        :param dev_y: validation data labels
        :param model_dir: the filename to store the optimal model weight
        :param num_epochs: maximum number of epochs
        :param patience: number of epochs with no improvement on validation set before stopping the training
        :param output_dir: record the training loss/validation loss after each training epoch
        :param weight_dir: the weight directory to load, used under "eval" mode. Under eval mode class Model will still
               act as a wrapper class for the LSTM_Attn class, such as turning inputs from keras input format to pytorch
               input format
        :param batch_size: the minibatch size of each training batch
        :param min_epochs: minimum number of epochs
        :param lambda_attn: the weight of attention KL loss
        :param lr: learning rates
        :param verbose: whether to print out the training loss and validation loss after each training epoch
        """
        # initializing parameters
        self.model = model
        self.mode = mode
        if self.mode == 'train':
            assert (train_X is not None and train_y is not None and tr_generator is None and num_steps is None) or \
                   (train_X is None and train_y is None and tr_generator is not None and num_steps is not None)
            if train_X is not None and train_y is not None:
                self.train_X, self.train_y, self.dev_X, self.dev_y = keras_input_to_pytorch_input(train_X), train_y, \
                                                             keras_input_to_pytorch_input(dev_X), dev_y
                self.tr_generator, self.num_steps = None, None
            else:
                self.tr_generator = tr_generator
                self.num_steps = num_steps
                self.dev_X, self.dev_y = keras_input_to_pytorch_input(dev_X), dev_y

            self.num_epochs, self.patience, self.min_epochs, self.batch_size = num_epochs, patience, min_epochs, batch_size
            self.model_dir = model_dir
            self.lambda_attn = lambda_attn
            self.output_dir = output_dir
            self.use_rationale = use_rationale
            self.rationale_with_UNK = rationale_with_UNK

            # define the loss functions here
            self.loss_function = nn.BCELoss()
            self.train_attn_loss = nn.KLDivLoss(reduction="sum")

            # define the optimizer and bind it to model parameters
            params = filter(lambda p: p.requires_grad, self.model.parameters())
            self.lr = lr
            self.verbose = verbose
            self.optimizer = optim.Adam(params, self.lr, weight_decay=0.001)
            self.trained_epochs = None
            self.validation_loss = []

        else:
            self.weight_dir = weight_dir
            self.model.eval()
            self.model.load_state_dict(torch.load(self.weight_dir))


    def train_one_batch(self, batch_X, batch_y):
        """
        Train the model on one minibatch data.
        """
        self.model.train()
        for idx in range(len(batch_X)):
            result = self.model(batch_X[idx])
            label = batch_y[idx]

            # loss for the predicting label component
            loss = self.loss_function(result['output'], Variable(torch.FloatTensor([label])))

            # loss for the attention component:
            # only compute loss of attention KL divergence if 1) the model uses attention 2) this tweet has ground truth
            # attention distribution
            if self.rationale_with_UNK == 1 and self.model.use_attn and batch_X[idx]['rationale_distr'] is not None and self.use_rationale:
                loss += self.lambda_attn * self.train_attn_loss(torch.log(result['attn']),
                                                                Variable(torch.FloatTensor([batch_X[idx]['rationale_distr']])))
            if self.rationale_with_UNK == 0 and self.model.use_attn and batch_X[idx]['rationale_exclude_UNK_distr'] is not None and self.use_rationale:
                loss += self.lambda_attn * self.train_attn_loss(torch.log(result['attn']), Variable(torch.FloatTensor(
                                                                    [batch_X[idx]['rationale_exclude_UNK_distr']])))

            loss.backward()
        self.optimizer.step()
        self.model.zero_grad()


    def train(self):
        """
        Trains the model, includes early stopping mechanism, stores optimal model state at local
        """
        optimal_loss = np.inf
        patience_count = 0
        optimal_model_state = None
        assert torch.get_num_threads() == 1

        for loop_idx in range(1, self.num_epochs + 1):
            print("Epoch %d: " % loop_idx)

            if self.tr_generator is not None: #use generator
                train_X = {}
                train_y = np.array([])
                for _ in range(self.num_steps):
                    tweet_X, truth_y = next(self.tr_generator)
                    for key in tweet_X:
                        if key not in train_X:
                            train_X[key] = deepcopy(tweet_X[key])
                        else:
                            train_X[key] = np.concatenate([train_X[key], tweet_X[key]])
                    train_y = np.concatenate([train_y, truth_y])
                self.train_X, self.train_y = keras_input_to_pytorch_input(train_X), train_y

            # create minibatches
            shuffle_idx = np.random.permutation(len(self.train_X))
            self.train_X = [self.train_X[i] for i in shuffle_idx]
            self.train_y = [self.train_y[i] for i in shuffle_idx]
            num_minibatches = len(self.train_X) / self.batch_size if len(self.train_X) % self.batch_size == 0 \
                    else len(self.train_X) // self.batch_size + 1

            # train minibatches
            for batch_idx in range(1, num_minibatches+1):
                self.train_one_batch(self.train_X[(batch_idx - 1) * self.batch_size: min(batch_idx * self.batch_size, len(self.train_X))],
                                     self.train_y[(batch_idx - 1) * self.batch_size: min(batch_idx * self.batch_size, len(self.train_X))])
                if batch_idx % 10 == 0 or batch_idx == num_minibatches:
                    print("Finish training %d/%d batches." % (batch_idx, num_minibatches))

            current_loss = self.compute_label_loss(self.dev_X, self.dev_y)
            print("Validation Loss: %.4f" % current_loss)
            self.validation_loss.append(current_loss)

            if self.verbose:
                with open(self.output_dir, 'a') as readme:
                    readme.write('Epoch %d: training loss: %.4f, validation loss: %.4f\n' % (loop_idx, training_loss, current_loss))

            optimal_loss = min(optimal_loss, current_loss)
            if optimal_loss == current_loss:
                optimal_model_state = deepcopy(self.model.state_dict())
                patience_count = 0
            else:
                patience_count += 1
            self.trained_epochs = loop_idx
            if loop_idx > self.min_epochs and patience_count >= self.patience:
                break
        torch.save(optimal_model_state, self.model_dir)


    def compute_label_loss(self, X, truth_y):
        """
        Compute label loss on the batch of data, excluding KL loss for attention consistency.
        """
        self.model.eval()
        loss = []
        for idx in range(len(X)):
            prediction = self.model(X[idx])
            label = truth_y[idx]
            loss.append(self.loss_function(prediction['output'], Variable(torch.FloatTensor([label]))).item())
        return np.mean(np.array(loss))


    def predict(self, batch_X, include_attention_weights=False):
        """
        Predict the scores of the batch of data.
        """
        batch_X = keras_input_to_pytorch_input(batch_X)
        self.model.eval()
        predictions = []
        if include_attention_weights:
            attention_weights = []
        for data in batch_X:
            predictions.append(self.model(data)['output'].item())
            if include_attention_weights:
                attention_weights.append(self.model(data)['attn'].detach().numpy()[0])

        if include_attention_weights is False:
            return np.array(predictions)
        else:
            return np.array(predictions), attention_weights


def keras_input_to_pytorch_input(X):
    # Reformat the input from keras input format to pytorch input format
    pytorch_X = []
    sample_key = next(iter(X.keys()))
    for record_idx in range(len(X[sample_key])):
        record = {}
        for key in X.keys():
            record[key] = X[key][record_idx]
        pytorch_X.append(record)
    return pytorch_X


if __name__ == '__main__':
    from LSTM_attn import _prelim_model

    attn_lstm = _prelim_model()
    # leave it as is to test train one batch functionality
    X1 = {'word_content_input':[1,10,53,12,0,0,0,0], 'splex_score_input':[35,12,61], 'cl_score_input':[64,19], 'rationale_distr':[0,0.5,0.2,0.3]}
    X2 = {'word_content_input':[12,20,83,352,367,124,0,0], 'splex_score_input':[25,42,21], 'cl_score_input':[90,9], 'rationale_distr':[0.2,0.1,0.2,0.1,0.2,0.2]}
    X3 = {'word_content_input':[120,220,583,3352,3367,0,0], 'splex_score_input':[5,72,41], 'cl_score_input':[6,72], 'rationale_distr':[0.1,0.2,0.1,0.1,0.5]}
    y1 = 0
    y2 = 1
    y3 = 1

    train_data_X = [X1, X2]
    dev_data_X = [X2]
    train_data_y = [y1, y2]
    dev_data_y = [y3]
    num_epochs, patience = 30, 3
    model_dir = '../model/optimal_weights'
    m = Model(attn_lstm, train_data_X, train_data_y, dev_data_X, dev_data_y, model_dir, output_dir="../model/README")
    m.train()
    print(m.predict(dev_data_X))