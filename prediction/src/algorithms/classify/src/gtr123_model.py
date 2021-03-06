import torch
import numpy as np
from torch.autograd import Variable
from torch import nn
import SimpleITK as sitk

from src.preprocess.gtr123_preprocess import lum_trans, resample

""""
Classification model from team gtr123
Code adapted from https://github.com/lfz/DSB2017
"""
config = {}

config['crop_size'] = [96, 96, 96]
config['scaleLim'] = [0.85, 1.15]
config['radiusLim'] = [6, 100]

config['stride'] = 4

config['detect_th'] = 0.05
config['conf_th'] = -1
config['nms_th'] = 0.05
config['filling_value'] = 160

config['startepoch'] = 20
config['lr_stage'] = np.array([50, 100, 140, 160])
config['lr'] = [0.01, 0.001, 0.0001, 0.00001]
config['miss_ratio'] = 1
config['miss_thresh'] = 0.03
config['anchors'] = [10, 30, 60]


class PostRes(nn.Module):
    """ """

    def __init__(self, n_in, n_out, stride=1):
        super(PostRes, self).__init__()
        self.conv1 = nn.Conv3d(n_in, n_out, kernel_size=3, stride=stride, padding=1)
        self.bn1 = nn.BatchNorm3d(n_out)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv3d(n_out, n_out, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm3d(n_out)

        if stride != 1 or n_out != n_in:
            self.shortcut = nn.Sequential(
                nn.Conv3d(n_in, n_out, kernel_size=1, stride=stride),
                nn.BatchNorm3d(n_out))
        else:
            self.shortcut = None

    def forward(self, x):

        residual = x
        if self.shortcut is not None:
            residual = self.shortcut(x)
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.bn2(out)

        out += residual
        out = self.relu(out)
        return out


class Net(nn.Module):
    """ """

    def __init__(self):
        super(Net, self).__init__()
        # The first few layers consumes the most memory, so use simple
        # convolution to save memory. Call these layers preBlock, i.e., before
        # the residual blocks of later layers.
        self.preBlock = nn.Sequential(
            nn.Conv3d(1, 24, kernel_size=3, padding=1),
            nn.BatchNorm3d(24),
            nn.ReLU(inplace=True),
            nn.Conv3d(24, 24, kernel_size=3, padding=1),
            nn.BatchNorm3d(24),
            nn.ReLU(inplace=True))

        # 3 poolings, each pooling downsamples the feature map by a factor 2.
        # 3 groups of blocks. The first block of each group has one pooling.
        num_blocks_forw = [2, 2, 3, 3]
        num_blocks_back = [3, 3]
        self.featureNum_forw = [24, 32, 64, 64, 64]
        self.featureNum_back = [128, 64, 64]

        for i in range(len(num_blocks_forw)):
            blocks = []

            for j in range(num_blocks_forw[i]):
                if j == 0:
                    blocks.append(PostRes(self.featureNum_forw[i], self.featureNum_forw[i + 1]))
                else:
                    blocks.append(PostRes(self.featureNum_forw[i + 1], self.featureNum_forw[i + 1]))

            setattr(self, 'forw' + str(i + 1), nn.Sequential(*blocks))

        for i in range(len(num_blocks_back)):
            blocks = []

            for j in range(num_blocks_back[i]):
                if j == 0:
                    if i == 0:
                        addition = 3
                    else:
                        addition = 0

                    blocks.append(PostRes(self.featureNum_back[i + 1] + self.featureNum_forw[i + 2] + addition,
                                          self.featureNum_back[i]))
                else:
                    blocks.append(PostRes(self.featureNum_back[i], self.featureNum_back[i]))

            setattr(self, 'back' + str(i + 2), nn.Sequential(*blocks))

        self.maxpool1 = nn.MaxPool3d(kernel_size=2, stride=2, return_indices=True)
        self.maxpool2 = nn.MaxPool3d(kernel_size=2, stride=2, return_indices=True)
        self.maxpool3 = nn.MaxPool3d(kernel_size=2, stride=2, return_indices=True)
        self.maxpool4 = nn.MaxPool3d(kernel_size=2, stride=2, return_indices=True)
        self.unmaxpool1 = nn.MaxUnpool3d(kernel_size=2, stride=2)
        self.unmaxpool2 = nn.MaxUnpool3d(kernel_size=2, stride=2)

        self.path1 = nn.Sequential(
            nn.ConvTranspose3d(64, 64, kernel_size=2, stride=2),
            nn.BatchNorm3d(64),
            nn.ReLU(inplace=True))

        self.path2 = nn.Sequential(
            nn.ConvTranspose3d(64, 64, kernel_size=2, stride=2),
            nn.BatchNorm3d(64),
            nn.ReLU(inplace=True))

        self.drop = nn.Dropout3d(p=0.2, inplace=False)
        self.output = nn.Sequential(nn.Conv3d(self.featureNum_back[0], 64, kernel_size=1),
                                    nn.ReLU(),
                                    # nn.Dropout3d(p = 0.3),
                                    nn.Conv3d(64, 5 * len(config['anchors']), kernel_size=1))

    def forward(self, x, coord):
        """

        Args:
          x:
          coord:

        Returns:

        """
        out = self.preBlock(x)  # 16
        out_pool, indices0 = self.maxpool1(out)
        out1 = self.forw1(out_pool)  # 32
        out1_pool, indices1 = self.maxpool2(out1)
        out2 = self.forw2(out1_pool)  # 64
        # out2 = self.drop(out2)
        out2_pool, indices2 = self.maxpool3(out2)
        out3 = self.forw3(out2_pool)  # 96
        out3_pool, indices3 = self.maxpool4(out3)
        out4 = self.forw4(out3_pool)  # 96
        # out4 = self.drop(out4)

        rev3 = self.path1(out4)
        comb3 = self.back3(torch.cat((rev3, out3), 1))  # 96+96
        # comb3 = self.drop(comb3)
        rev2 = self.path2(comb3)

        feat = self.back2(torch.cat((rev2, out2, coord), 1))  # 64+64
        comb2 = self.drop(feat)
        out = self.output(comb2)
        size = out.size()
        out = out.view(out.size(0), out.size(1), -1)
        # out = out.transpose(1, 4).transpose(1, 2).transpose(2, 3).contiguous()
        out = out.transpose(1, 2).contiguous().view(size[0], size[2], size[3], size[4], len(config['anchors']), 5)
        # out = out.view(-1, 5)
        return feat, out


class CaseNet(nn.Module):
    """The classification Net from the gtr123 team - part of the Winning algorithm for DSB2017"""

    def __init__(self):
        super(CaseNet, self).__init__()
        self.NoduleNet = Net()
        self.fc1 = nn.Linear(128, 64)
        self.fc2 = nn.Linear(64, 1)
        self.pool = nn.MaxPool3d(kernel_size=2)
        self.dropout = nn.Dropout(0.5)
        self.baseline = nn.Parameter(torch.Tensor([-30.0]).float())
        self.Relu = nn.ReLU()

    def forward(self, xlist, coordlist):
        """

        Args:
          xlist:  Image of size n x k x 1x 96 x 96 x 96
          coordlist: Coordinates of size n x k x 3 x 24 x 24 x 24

        Returns:

        """
        xsize = xlist.size()
        corrdsize = coordlist.size()
        print(xsize)
        # xlist = xlist.view(-1,xsize[2],xsize[3],xsize[4],xsize[5])
        # coordlist = coordlist.view(-1,corrdsize[2],corrdsize[3],corrdsize[4],corrdsize[5])

        noduleFeat, nodulePred = self.NoduleNet(xlist, coordlist)
        nodulePred = nodulePred.contiguous().view(corrdsize[0], corrdsize[1], -1)

        featshape = noduleFeat.size()  # nk x 128 x 24 x 24 x24
        centerFeat = self.pool(noduleFeat[:, :, featshape[2] // 2 - 1:featshape[2] // 2 + 1,
                               featshape[3] // 2 - 1:featshape[3] // 2 + 1,
                               featshape[4] // 2 - 1:featshape[4] // 2 + 1])
        centerFeat = centerFeat[:, :, 0, 0, 0]
        out = self.dropout(centerFeat)
        out = self.Relu(self.fc1(out))
        out = torch.sigmoid(self.fc2(out))
        out = out.view(xsize[0], xsize[1])
        base_prob = torch.sigmoid(self.baseline)
        casePred = 1 - torch.prod(1 - out, dim=1) * (1 - base_prob.expand(out.size()[0]))
        return nodulePred, casePred, out


class SimpleCrop(object):
    """ """

    def __init__(self):
        self.crop_size = config['crop_size']
        self.scaleLim = config['scaleLim']
        self.radiusLim = config['radiusLim']
        self.stride = config['stride']
        self.filling_value = config['filling_value']

    def __call__(self, imgs, target):
        crop_size = np.array(self.crop_size).astype('int')

        start = (target[:3] - crop_size / 2).astype('int')
        pad = [[0, 0]]

        for i in range(3):
            if start[i] < 0:
                leftpad = -start[i]
                start[i] = 0
            else:
                leftpad = 0
            if start[i] + crop_size[i] > imgs.shape[i + 1]:
                rightpad = start[i] + crop_size[i] - imgs.shape[i + 1]
            else:
                rightpad = 0

            pad.append([leftpad, rightpad])

        imgs = np.pad(imgs, pad, 'constant', constant_values=self.filling_value)
        crop = imgs[:, start[0]:start[0] + crop_size[0], start[1]:start[1] + crop_size[1],
                    start[2]:start[2] + crop_size[2]]

        normstart = np.array(start).astype('float32') / np.array(imgs.shape[1:]) - 0.5
        normsize = np.array(crop_size).astype('float32') / np.array(imgs.shape[1:])
        xx, yy, zz = np.meshgrid(np.linspace(normstart[0], normstart[0] + normsize[0], self.crop_size[0] / self.stride),
                                 np.linspace(normstart[1], normstart[1] + normsize[1], self.crop_size[1] / self.stride),
                                 np.linspace(normstart[2], normstart[2] + normsize[2], self.crop_size[2] / self.stride),
                                 indexing='ij')
        coord = np.concatenate([xx[np.newaxis, ...], yy[np.newaxis, ...], zz[np.newaxis, :]], 0).astype('float32')

        return crop, coord


def predict(image_itk, nodule_list, model_path="src/algorithms/classify/assets/gtr123_model.ckpt"):
    """

    Args:
      image_itk: ITK dicom image
      nodule_list: List of nodules
      model_path: Path to the torch model (Default value = "src/algorithms/classify/assets/gtr123_model.ckpt")

    Returns:
      List of nodules, and probabilities

    """
    if not nodule_list:
        return []
    casenet = CaseNet()

    casenet.load_state_dict(torch.load(model_path))
    casenet.eval()

    if torch.cuda.is_available():
        casenet = torch.nn.DataParallel(casenet).cuda()
    # else:
        # casenet = torch.nn.parallel.DistributedDataParallel(casenet)

    image = sitk.GetArrayFromImage(image_itk)
    spacing = np.array(image_itk.GetSpacing())[::-1]
    image = lum_trans(image)
    image = resample(image, spacing, np.array([1, 1, 1]), order=1)[0]

    crop = SimpleCrop()

    results = []
    for nodule in nodule_list:
        print(nodule)
        nod_location = np.array([np.float32(nodule[s]) for s in ["z", "y", "x"]])
        nod_location *= spacing
        cropped_image, coords = crop(image[np.newaxis], nod_location)
        cropped_image = Variable(torch.from_numpy(cropped_image[np.newaxis]).float())
        cropped_image.volatile = True
        coords = Variable(torch.from_numpy(coords[np.newaxis]).float())
        coords.volatile = True
        _, pred, _ = casenet(cropped_image, coords)
        results.append(
            {"x": nodule["x"], "y": nodule["y"], "z": nodule["z"], "p_concerning": float(pred.data.cpu().numpy())})

    return results
