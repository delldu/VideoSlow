#!/usr/bin/env python3
import argparse
import os
import os.path
import ctypes
from shutil import rmtree, move
from PIL import Image
import torch
import torchvision.transforms as transforms
import model
import dataloader
import platform
from tqdm import tqdm
from apex import amp

import pdb

# For parsing commandline arguments
parser = argparse.ArgumentParser()
parser.add_argument("--ffmpeg_dir", type=str, default="", help='path to ffmpeg.exe')
parser.add_argument("--video", type=str, required=True, help='path of video to be converted')
parser.add_argument("--checkpoint", type=str, required=True, help='path of checkpoint for pretrained model')
parser.add_argument("--fps", type=float, default=30, help='specify fps of output video. Default: 30.')
parser.add_argument("--sf", type=int, required=True, help='specify the slomo factor N. This will increase the frames by Nx. Example sf=2 ==> 2x frames')
parser.add_argument("--output", type=str, default="output.mkv", help='Specify output file name. Default: output.mp4')
args = parser.parse_args()

# python video_to_slomo.py --checkpoint models/SuperSloMo.ckpt --sf 4 --video /tmp/a.mp4 --output /tmp/b.mkv


def check():
    """
    Checks the validity of commandline arguments.

    Parameters
    ----------
        None

    Returns
    -------
        error : string
            Error message if error occurs otherwise blank string.
    """


    error = ""
    if (args.sf < 2):
        error = "Error: --sf/slomo factor has to be atleast 2"
    if (args.fps < 1):
        error = "Error: --fps has to be atleast 1"
    if ".mkv" not in args.output:
        error = "output needs to have mkv container"
    return error

def extract_frames(video, outDir):
    """
    Converts the `video` to images.

    Parameters
    ----------
        video : string
            full path to the video file.
        outDir : string
            path to directory to output the extracted images.

    Returns
    -------
        error : string
            Error message if error occurs otherwise blank string.
    """


    error = ""
    print('{} -i {} -vsync 0 {}/%06d.png'.format(os.path.join(args.ffmpeg_dir, "ffmpeg"), video, outDir))
    retn = os.system('{} -i "{}" -vsync 0 {}/%06d.png'.format(os.path.join(args.ffmpeg_dir, "ffmpeg"), video, outDir))
    if retn:
        error = "Error converting file:{}. Exiting.".format(video)
    return error

def create_video(dir):
    error = ""
    print('{} -r {} -i {}/%d.png -vcodec ffvhuff {}'.format(os.path.join(args.ffmpeg_dir, "ffmpeg"), args.fps, dir, args.output))
    retn = os.system('{} -r {} -i {}/%d.png -vcodec ffvhuff "{}"'.format(os.path.join(args.ffmpeg_dir, "ffmpeg"), args.fps, dir, args.output))
    if retn:
        error = "Error creating output video. Exiting."
    return error


def main():
    # Check if arguments are okay
    error = check()
    if error:
        print(error)
        exit(1)

    # Create extraction folder and extract frames
    # Assuming UNIX-like system where "." indicates hidden directories
    extractionDir = ".tmpSuperSloMo"
    # if os.path.isdir(extractionDir):
    #     rmtree(extractionDir)
    # os.mkdir(extractionDir)

    extractionPath = os.path.join(extractionDir, "input")
    outputPath     = os.path.join(extractionDir, "output")
    # os.mkdir(extractionPath)
    # os.mkdir(outputPath)
    # error = extract_frames(args.video, extractionPath)
    # if error:
    #     print(error)
    #     exit(1)

    # Initialize transforms
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    mean = [0.429, 0.431, 0.397]
    std  = [1, 1, 1]
    normalize = transforms.Normalize(mean=mean,
                                     std=std)

    negmean = [x * -1 for x in mean]
    revNormalize = transforms.Normalize(mean=negmean, std=std)

    # Temporary fix for issue #7 https://github.com/avinashpaliwal/Super-SloMo/issues/7 -
    # - Removed per channel mean subtraction for CPU.
    if (device == "cpu"):
        transform = transforms.Compose([transforms.ToTensor()])
        TP = transforms.Compose([transforms.ToPILImage()])
    else:
        transform = transforms.Compose([transforms.ToTensor(), normalize])
        TP = transforms.Compose([revNormalize, transforms.ToPILImage()])

    # Load data
    videoFrames = dataloader.Video(root=extractionPath, transform=transform)
    # pdb.set_trace()
    # len(videoFrames[0]) ==> 2
    # (Pdb) videoFrames[0][0].size()
    # torch.Size([3, 512, 960])


    videoFramesloader = torch.utils.data.DataLoader(videoFrames, batch_size=1, shuffle=False)

    # Initialize model
    # UNet(inChannels, outChannels)
    # flow Computation !!!
    flowComp = model.UNet(6, 4)
    flowComp.to(device)
    for param in flowComp.parameters():
        param.requires_grad = False

    # arbitary-time
    ArbTimeFlowIntrp = model.UNet(20, 5)
    ArbTimeFlowIntrp.to(device)
    for param in ArbTimeFlowIntrp.parameters():
        param.requires_grad = False

    flowBackWarp = model.backWarp(videoFrames.dim[0], videoFrames.dim[1], device)
    flowBackWarp = flowBackWarp.to(device)
    flowBackWarp = amp.initialize(flowBackWarp, opt_level= "O1")
    # pdb.set_trace()
    # (Pdb) videoFrames.dim[0], videoFrames.dim[1]
    # (960, 512)

    dict1 = torch.load(args.checkpoint, map_location='cpu')
    # dict_keys(['Detail', 'epoch', 'timestamp', 'trainBatchSz', 'validationBatchSz', 
    # 'learningRate', 'loss', 'valLoss', 'valPSNR', 'state_dictFC', 'state_dictAT'])

    ArbTimeFlowIntrp.load_state_dict(dict1['state_dictAT'])
    flowComp.load_state_dict(dict1['state_dictFC'])

    ArbTimeFlowIntrp = amp.initialize(ArbTimeFlowIntrp, opt_level= "O1")
    flowComp = amp.initialize(flowComp, opt_level= "O1")

    # Interpolate frames
    frameCounter = 1

    with torch.no_grad():
        for _, (frame0, frame1) in enumerate(tqdm(videoFramesloader), 0):

            I0 = frame0.to(device)
            I1 = frame1.to(device)
            # pdb.set_trace()
            # torch.Size([1, 3, 512, 960])

            flowOut = flowComp(torch.cat((I0, I1), dim=1))
            F_0_1 = flowOut[:,:2,:,:]
            F_1_0 = flowOut[:,2:,:,:]
            # (Pdb) pp flowOut.size()
            # torch.Size([1, 4, 512, 960])
            # (Pdb) pp F_0_1.size()
            # torch.Size([1, 2, 512, 960])
            # (Pdb) pp F_1_0.size()
            # torch.Size([1, 2, 512, 960])

            # pdb.set_trace()
            # (Pdb) pp I0.size(), I1.size()
            # (torch.Size([1, 3, 512, 960]), torch.Size([1, 3, 512, 960]))
            # (Pdb) pp torch.cat((I0, I1), dim=1).size()
            # torch.Size([1, 6, 512, 960])

            # Save reference frames in output folder
            (TP(frame0[0].detach())).resize(videoFrames.origDim, Image.BILINEAR).save(\
                os.path.join(outputPath, "{:06d}.png".format(frameCounter)))
            frameCounter += 1

            # Generate intermediate frames
            # (Pdb) for i in range(1, args.sf): print(i)
            # 1
            # 2
            # 3
            for intermediateIndex in range(1, args.sf):
                t = float(intermediateIndex) / args.sf
                temp = -t * (1 - t)
                fCoeff = [temp, t * t, (1 - t) * (1 - t), temp]

                pdb.set_trace()
                # (Pdb) pp temp
                # -0.1875
                # (Pdb) pp fCoeff
                # [-0.1875, 0.0625, 0.5625, -0.1875]

                F_t_0 = fCoeff[0] * F_0_1 + fCoeff[1] * F_1_0
                F_t_1 = fCoeff[2] * F_0_1 + fCoeff[3] * F_1_0

                g_I0_F_t_0 = flowBackWarp(I0, F_t_0)
                g_I1_F_t_1 = flowBackWarp(I1, F_t_1)

                intrpOut = ArbTimeFlowIntrp(\
                    torch.cat((I0, I1, F_0_1, F_1_0, F_t_1, F_t_0, \
                        g_I1_F_t_1, g_I0_F_t_0), \
                    dim=1))

                F_t_0_f = intrpOut[:, :2, :, :] + F_t_0
                F_t_1_f = intrpOut[:, 2:4, :, :] + F_t_1
                pdb.set_trace()

                # pdb.set_trace()
                # (Pdb) intrpOut.size()
                # torch.Size([1, 5, 512, 960])

                V_t_0   = torch.sigmoid(intrpOut[:, 4:5, :, :])
                V_t_1   = 1 - V_t_0

                g_I0_F_t_0_f = flowBackWarp(I0, F_t_0_f)
                g_I1_F_t_1_f = flowBackWarp(I1, F_t_1_f)

                # pdb.set_trace()

                wCoeff = [1 - t, t]

                Ft_p = (wCoeff[0] * V_t_0 * g_I0_F_t_0_f + wCoeff[1] * V_t_1 * g_I1_F_t_1_f) / (wCoeff[0] * V_t_0 + wCoeff[1] * V_t_1)

                del g_I0_F_t_0_f, g_I1_F_t_1_f, F_t_0_f, F_t_1_f, F_t_0, F_t_1, intrpOut, V_t_0, V_t_1, wCoeff
                torch.cuda.empty_cache()

                pdb.set_trace()

                # Save intermediate frame
                (TP(Ft_p[0].cpu().detach())).resize(videoFrames.origDim, Image.BILINEAR).save(os.path.join(outputPath, "{:06d}.png".format(frameCounter)))
                del Ft_p
                torch.cuda.empty_cache()

                frameCounter += 1

            del F_0_1, F_1_0, flowOut, I0, I1, frame0, frame1
            torch.cuda.empty_cache()

    # Generate video from interpolated frames
    # create_video(outputPath)

    # Remove temporary files
    # rmtree(extractionDir)

    exit(0)

main()
