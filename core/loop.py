from collections import defaultdict

import torch
from torch.nn import functional as F

from .callbacks import CallbackGroup


class Loop:
    """
    Simple training loop implementation.

    The loop contains two phases: training and validation. Each phase is
    computed on a separate dataset, and tracks its own parameters, like,
    average loss and batch number.

    Parameters:
        model: An optimized model.

        alpha: A value of weight used to perform linear interpolation between
            loss on the previous epoch and the new epoch, like:

                new_loss = old_loss*alpha + (1 - alpha)*new_loss

    """
    def __init__(self, model, optimizer, schedule, alpha: float=0.98,
                 move_to_device=True, device=None):

        if move_to_device:
            device = torch.device(device or 'cpu')
            model = model.to(device)
            self.device = device

        self.model = model
        self.optimizer = optimizer
        self.schedule = schedule
        self.alpha = alpha
        self.move_to_device = move_to_device
        self.stop = False
        self.callbacks = None
        self.stepper = None

    def run(self, train_data, valid_data=None, loss_fn=F.nll_loss,
            epochs: int=100, callbacks=None, metrics=None):

        phases = [Phase(name='train', dataset=train_data)]
        if valid_data is not None:
            phases.append(Phase(name='valid', dataset=valid_data))

        cb = CallbackGroup(callbacks)
        cb.set_loop(self)
        cb.training_start()
        self.callbacks = cb
        self.stepper = self.make_stepper(loss_fn, metrics)

        for epoch in range(epochs):
            if self.stop:
                break
            metrics = {}
            cb.epoch_start(epoch)
            for phase in phases:
                cb.epoch_start(epoch)
                is_training = phase.name == 'train'
                for batch in phase.dataset:
                    x, y = self._place_and_unwrap_if_needed(batch)
                    phase.batch_num += 1
                    cb.batch_start(epoch, phase)
                    batch_metrics = self.stepper.step(x, y, is_training)
                    self.update_metrics(phase, batch_metrics)
                    cb.batch_end(epoch, phase)
                metrics.update({
                    f'{phase.name}_{k}': v
                    for k, v in phase.metrics.items()})
            cb.epoch_end(epoch, metrics)
        cb.training_end()

    def make_stepper(self, loss_fn, metrics=None, stepper=None):
        stepper_cls = stepper or Stepper
        inst = stepper_cls(
            self.model, self.optimizer, self.schedule, loss_fn, metrics)
        return inst

    def save_model(self, path):
        self.stepper.save_model(path)

    def update_metrics(self, phase, batch_metrics):
        a = self.alpha
        updated = {}
        for name, new_value in batch_metrics.items():
            old_value = phase.rolling_metrics[name]
            avg_value = a*old_value + (1 - a)*new_value
            debias_value = avg_value/(1 - a**phase.batch_num)
            updated[name] = debias_value
            phase.rolling_metrics[name] = avg_value
        phase.metrics = updated

    @property
    def lr_schedule(self):
        return self.stepper.learning_rates

    def _place_and_unwrap_if_needed(self, batch):
        x, *y = batch
        if self.move_to_device:
            x = x.to(self.device)
            y = [tensor.to(self.device) for tensor in y]
        else:
            x, *y = batch
        if len(y) == 1:
            [y] = y
        return x, y

    def __getitem__(self, item):
        return self.callbacks[item]


class Phase:
    """
    Model training loop phase.

    Each model's training loop iteration could be separated into (at least) two
    phases: training and validation. The instances of this class track
    metrics and counters, related to the specific phase, and keep the reference
    to subset of data, used during phase.
    """
    def __init__(self, name: str, dataset):
        self.name = name
        self.dataset = dataset
        self.batch_num = 0
        self.rolling_metrics = defaultdict(lambda: 0)
        self.metrics = None

    def __repr__(self):
        if self.metrics is None:
            return f'<Phase: {self.name}, metrics: none>'
        metrics = ', '.join([
            f'{key}={value:2.4f}'
            for key, value in self.metrics.items()])
        return f'<Phase: {self.name}, metrics: {metrics}>'


class Stepper:
    """
    A thin wrapper encapsulating the model, its optimizer, a scheduler, and a
    loss function into single object.

    The stepper instance is invoked during each training iteration and returns
    the loss on batch.
    """
    def __init__(self, model, optimizer, schedule, loss, metrics=None):
        if schedule.last_epoch == -1:
            schedule.step()
        self.model = model
        self.optimizer = optimizer
        self.schedule = schedule
        self.loss = loss
        self.metrics = metrics
        self.learning_rates = []

    def step(self, x, y, train: bool=True):
        """
        Performs a single training step.

        Args:
            x: Features tensor.
            y: Target tensor.
            train: If False, then the gradient is not computed, and the model's
                parameters are not updated.

        Returns:
            loss: The loss value on batch.

        """
        metrics = {}
        self.model.train(train)

        with torch.set_grad_enabled(train):
            out = self.model(x)
            loss = self.loss(out, y)
            metrics['loss'] = loss.item()

            if self.metrics is not None:
                for metric in self.metrics:
                    metrics[metric.__name__] = metric(out.cpu(), y.cpu())

            if train:
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                self.schedule.step()
                lrs = self.schedule.get_lr()
                self.learning_rates.append(lrs)

        return metrics

    def save_model(self, path: str):
        """
        Saves model state into file.
        """
        torch.save(self.model.state_dict(), path)
